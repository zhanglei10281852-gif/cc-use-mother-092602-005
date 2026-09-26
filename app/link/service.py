from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.link.repository import LinkRepository

SCHEMA = """
CREATE TABLE IF NOT EXISTS link_timeslices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    total_kbps INTEGER NOT NULL CHECK(total_kbps > 0),
    price_per_kbps_s REAL NOT NULL DEFAULT 0 CHECK(price_per_kbps_s >= 0),
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed','settled')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS link_tenant_quotas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant TEXT NOT NULL UNIQUE,
    max_kbps_per_slice INTEGER NOT NULL CHECK(max_kbps_per_slice >= 0),
    max_active_reservations INTEGER NOT NULL CHECK(max_active_reservations >= 0),
    updated_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS link_priority_classes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    rank INTEGER NOT NULL CHECK(rank BETWEEN 0 AND 1000),
    may_preempt INTEGER NOT NULL DEFAULT 0 CHECK(may_preempt IN (0,1)),
    description TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS link_reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant TEXT NOT NULL,
    message_key TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    priority_class TEXT NOT NULL,
    priority_rank INTEGER NOT NULL,
    kbps INTEGER NOT NULL CHECK(kbps > 0),
    timeslice_id INTEGER REFERENCES link_timeslices(id),
    status TEXT NOT NULL CHECK(status IN ('reserved','deferred','released','preempted','rejected','expired')),
    status_reason TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tenant, message_key)
);
CREATE INDEX IF NOT EXISTS idx_link_reservations_slice ON link_reservations(timeslice_id, status);
CREATE TABLE IF NOT EXISTS link_usage_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id INTEGER NOT NULL REFERENCES link_reservations(id) ON DELETE CASCADE,
    tenant TEXT NOT NULL,
    timeslice_id INTEGER NOT NULL REFERENCES link_timeslices(id),
    kbps INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_link_ledger_slice ON link_usage_ledger(timeslice_id, ended_at);
CREATE INDEX IF NOT EXISTS idx_link_ledger_reservation ON link_usage_ledger(reservation_id, id);
CREATE TABLE IF NOT EXISTS link_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision TEXT NOT NULL,
    actor TEXT NOT NULL,
    reservation_id INTEGER,
    tenant TEXT NOT NULL DEFAULT '',
    timeslice_id INTEGER,
    reason TEXT NOT NULL DEFAULT '',
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_link_decisions_slice ON link_decisions(timeslice_id, id);
CREATE INDEX IF NOT EXISTS idx_link_decisions_reservation ON link_decisions(reservation_id, id);
CREATE TABLE IF NOT EXISTS link_bills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timeslice_id INTEGER NOT NULL REFERENCES link_timeslices(id),
    tenant TEXT NOT NULL,
    kbps_seconds INTEGER NOT NULL,
    amount REAL NOT NULL,
    details_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    UNIQUE(timeslice_id, tenant)
);
"""


def ensure_schema() -> None:
    with transaction(immediate=True) as connection:
        connection.executescript(SCHEMA)


def digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


class LinkCoordinationService:
    """协调卫星地面链路的时间片、租户配额、消息优先级、预留、抢占、延期、迁移与结算。

    明确规则：
    - 预留按 (tenant, message_key) 幂等，重试返回既有记录，不重复扣减带宽。
    - 先校验租户配额再考虑抢占，紧急遥测也不能造成配额透支。
    - 自动抢占仅在优先级类别允许且 rank 严格更高时触发；按 rank 升序、创建时间
      降序逐个抢占，若抢占全部低优先级预留仍不足，则不执行任何抢占并记录拒绝。
    - 延期与跨片迁移只做配额与容量校验，不触发抢占。
    - rollover 按 rank 降序、创建时间升序迁移；容纳不下转为 deferred，无后继
      时间片则标记 expired。
    - 结算仅允许 closed 时间片，账单 = Σ kbps × 占用秒数 × 时间片单价。
    """

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = LinkRepository(self.connection)

    # ---------- 登记 ----------

    def create_timeslice(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        starts_at = self._normalize(payload["starts_at"])
        ends_at = self._normalize(payload["ends_at"])
        if ends_at <= starts_at:
            raise ValidationError("时间片结束时刻必须晚于开始时刻")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = LinkRepository(connection)
            if repository.timeslice_by_code(payload["code"]):
                raise ConflictError("链路时间片编码已存在")
            return repository.create_timeslice(
                code=payload["code"], starts_at=to_storage(starts_at), ends_at=to_storage(ends_at),
                total_kbps=payload["total_kbps"], price_per_kbps_s=payload["price_per_kbps_s"],
                created_by=actor, now=now,
            )

    def list_timeslices(self, status: str | None = None) -> list[dict[str, Any]]:
        usage = self.repository.usage_summary()
        items: list[dict[str, Any]] = []
        for row in self.repository.list_timeslices(status):
            reserved = usage.get(row["id"], 0)
            items.append({**row, "reserved_kbps": reserved, "available_kbps": row["total_kbps"] - reserved})
        return items

    def get_timeslice(self, timeslice_id: int) -> dict[str, Any]:
        row = self.repository.timeslice_by_id(timeslice_id)
        if row is None:
            raise NotFoundError("链路时间片不存在")
        result = dict(row)
        result["snapshot"] = self._snapshot(self.repository, timeslice_id)
        return result

    def set_quota(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            return LinkRepository(connection).upsert_quota(actor=actor, now=now, **payload)

    def list_quotas(self) -> list[dict[str, Any]]:
        return self.repository.list_quotas()

    def create_priority_class(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = LinkRepository(connection)
            if repository.priority_class_by_code(payload["code"]):
                raise ConflictError("消息优先级类别编码已存在")
            return repository.create_priority_class(
                code=payload["code"], rank=payload["rank"], may_preempt=1 if payload["may_preempt"] else 0,
                description=payload["description"], created_by=actor, now=now,
            )

    def list_priority_classes(self) -> list[dict[str, Any]]:
        return self.repository.list_priority_classes()

    # ---------- 预留生命周期 ----------

    def reserve(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        request_digest = digest({
            "tenant": payload["tenant"], "message_key": payload["message_key"],
            "priority_class": payload["priority_class"], "kbps": payload["kbps"],
            "timeslice_id": payload["timeslice_id"],
        })
        with transaction(immediate=True) as connection:
            repository = LinkRepository(connection)
            existing = repository.reservation_by_key(payload["tenant"], payload["message_key"])
            if existing is not None:
                if existing["payload_digest"] != request_digest:
                    raise ConflictError("同一消息键对应了不同的预留参数")
                return dict(existing)
            timeslice = repository.timeslice_by_id(payload["timeslice_id"])
            if timeslice is None:
                raise NotFoundError("链路时间片不存在")
            priority = repository.priority_class_by_code(payload["priority_class"])
            if priority is None:
                raise NotFoundError("消息优先级类别未登记")
            rejection: str | None = None
            if timeslice["status"] != "open":
                rejection = f"时间片已关闭或已结算（状态 {timeslice['status']}），未开放预留"
            if rejection is None:
                rejection = self._quota_rejection(repository, timeslice["id"], payload["tenant"], payload["kbps"])
            if rejection is None:
                available = timeslice["total_kbps"] - repository.reserved_kbps(timeslice["id"])
                if available < payload["kbps"]:
                    rejection = self._auto_preempt(repository, timeslice, priority, payload, now)
            status = "reserved" if rejection is None else "rejected"
            row = repository.create_reservation(
                tenant=payload["tenant"], message_key=payload["message_key"], payload_digest=request_digest,
                priority_class=payload["priority_class"], priority_rank=priority["rank"], kbps=payload["kbps"],
                timeslice_id=timeslice["id"], status=status, status_reason=rejection or "", now=now,
            )
            if status == "reserved":
                repository.open_ledger(reservation_id=row["id"], tenant=row["tenant"], timeslice_id=timeslice["id"], kbps=row["kbps"], now=now)
            self._record(
                repository, decision="reserve" if status == "reserved" else "reject", actor=payload["tenant"],
                reservation_id=row["id"], tenant=payload["tenant"], timeslice_id=timeslice["id"],
                reason=rejection or "预留成功",
            )
            return dict(repository.reservation_by_id(row["id"]))

    def release(self, reservation_id: int, actor: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = LinkRepository(connection)
            row = repository.reservation_by_id(reservation_id)
            if row is None:
                raise NotFoundError("链路预留不存在")
            if row["status"] == "released":
                return dict(row)
            if row["status"] not in {"reserved", "deferred"}:
                raise ConflictError(f"当前状态 {row['status']} 不允许释放")
            if row["status"] == "reserved":
                repository.close_ledger(row["id"], now, "release")
            repository.set_reservation_status(row["id"], status="released", reason=reason, now=now)
            self._record(repository, decision="release", actor=actor, reservation_id=row["id"], tenant=row["tenant"], timeslice_id=row["timeslice_id"], reason=reason)
            return dict(repository.reservation_by_id(row["id"]))

    def preempt(self, reservation_id: int, actor: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = LinkRepository(connection)
            row = repository.reservation_by_id(reservation_id)
            if row is None:
                raise NotFoundError("链路预留不存在")
            if row["status"] != "reserved":
                raise ConflictError(f"只有预留中的记录可以被抢占，当前状态 {row['status']}")
            repository.close_ledger(row["id"], now, "preempt")
            repository.set_reservation_status(row["id"], status="preempted", reason=reason, now=now)
            self._record(repository, decision="preempt", actor=actor, reservation_id=row["id"], tenant=row["tenant"], timeslice_id=row["timeslice_id"], reason=reason)
            return dict(repository.reservation_by_id(row["id"]))

    def defer(self, reservation_id: int, actor: str, reason: str, target_timeslice_id: int) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = LinkRepository(connection)
            row = repository.reservation_by_id(reservation_id)
            if row is None:
                raise NotFoundError("链路预留不存在")
            if row["status"] not in {"reserved", "deferred"}:
                raise ConflictError(f"当前状态 {row['status']} 不允许延期")
            target = repository.timeslice_by_id(target_timeslice_id)
            if target is None:
                raise NotFoundError("目标链路时间片不存在")
            if target["status"] != "open":
                raise ConflictError("目标时间片未开放")
            if row["status"] == "reserved" and row["timeslice_id"] == target["id"]:
                raise ConflictError("目标时间片与当前时间片相同")
            if row["status"] == "reserved":
                repository.close_ledger(row["id"], now, "defer")
            rejection = self._placement_rejection(repository, target, row["tenant"], int(row["kbps"]))
            if rejection is None:
                repository.move_reservation(row["id"], timeslice_id=target["id"], status="reserved", reason="", now=now)
                repository.open_ledger(reservation_id=row["id"], tenant=row["tenant"], timeslice_id=target["id"], kbps=row["kbps"], now=now)
                decision_reason = reason
            else:
                repository.move_reservation(row["id"], timeslice_id=None, status="deferred", reason=rejection, now=now)
                decision_reason = f"{reason}；安置失败：{rejection}"
            self._record(repository, decision="defer", actor=actor, reservation_id=row["id"], tenant=row["tenant"], timeslice_id=target["id"], reason=decision_reason)
            return dict(repository.reservation_by_id(row["id"]))

    # ---------- 时间片生命周期 ----------

    def rollover(self, timeslice_id: int, actor: str, target_timeslice_id: int | None = None, reason: str = "时间片结束，迁移未完成传输") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = LinkRepository(connection)
            source = repository.timeslice_by_id(timeslice_id)
            if source is None:
                raise NotFoundError("链路时间片不存在")
            if source["status"] != "open":
                raise ConflictError(f"时间片状态为 {source['status']}，不能执行迁移")
            target: sqlite3.Row | None = None
            if target_timeslice_id is not None:
                target = repository.timeslice_by_id(target_timeslice_id)
                if target is None:
                    raise NotFoundError("目标链路时间片不存在")
                if target["id"] == source["id"]:
                    raise ValidationError("目标时间片不能与源时间片相同")
                if target["status"] != "open":
                    raise ConflictError("目标时间片未开放")
            else:
                target = repository.next_open_timeslice(source["id"])
            repository.set_timeslice_status(source["id"], "closed", now)
            migrated: list[int] = []
            deferred: list[int] = []
            expired: list[int] = []
            for row in repository.active_reservations(source["id"]):
                repository.close_ledger(row["id"], now, "rollover")
                if target is None:
                    message = "源时间片已关闭且不存在可用的后继时间片"
                    repository.move_reservation(row["id"], timeslice_id=None, status="expired", reason=message, now=now)
                    expired.append(int(row["id"]))
                    self._record(repository, decision="migrate", actor=actor, reservation_id=row["id"], tenant=row["tenant"], timeslice_id=source["id"], reason=message)
                    continue
                rejection = self._placement_rejection(repository, target, row["tenant"], int(row["kbps"]))
                if rejection is None:
                    repository.move_reservation(row["id"], timeslice_id=target["id"], status="reserved", reason="", now=now)
                    repository.open_ledger(reservation_id=row["id"], tenant=row["tenant"], timeslice_id=target["id"], kbps=row["kbps"], now=now)
                    migrated.append(int(row["id"]))
                    self._record(repository, decision="migrate", actor=actor, reservation_id=row["id"], tenant=row["tenant"], timeslice_id=target["id"], reason=f"{reason}：迁入 {target['code']}")
                else:
                    repository.move_reservation(row["id"], timeslice_id=None, status="deferred", reason=rejection, now=now)
                    deferred.append(int(row["id"]))
                    self._record(repository, decision="migrate", actor=actor, reservation_id=row["id"], tenant=row["tenant"], timeslice_id=target["id"], reason=rejection)
            return {
                "timeslice": dict(repository.timeslice_by_id(source["id"])),
                "target_timeslice_id": target["id"] if target else None,
                "migrated": migrated,
                "deferred": deferred,
                "expired": expired,
            }

    def settle(self, timeslice_id: int, actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = LinkRepository(connection)
            timeslice = repository.timeslice_by_id(timeslice_id)
            if timeslice is None:
                raise NotFoundError("链路时间片不存在")
            if timeslice["status"] == "open":
                raise ConflictError("时间片仍在开放，请先执行 rollover 关闭并迁移未完成传输")
            if timeslice["status"] == "settled":
                raise ConflictError("时间片已结算，不能重复结算")
            repository.close_open_ledger_entries(timeslice["id"], now, "settle")
            aggregates: dict[str, dict[str, Any]] = {}
            for entry in repository.ledger_entries(timeslice["id"]):
                seconds = max(0, int((from_storage(entry["ended_at"]) - from_storage(entry["started_at"])).total_seconds()))
                slot = aggregates.setdefault(entry["tenant"], {"kbps_seconds": 0, "reservations": {}})
                slot["kbps_seconds"] += int(entry["kbps"]) * seconds
                item = slot["reservations"].setdefault(
                    entry["reservation_id"],
                    {"reservation_id": entry["reservation_id"], "message_key": entry["message_key"], "kbps": entry["kbps"], "seconds": 0},
                )
                item["seconds"] += seconds
            bills: list[dict[str, Any]] = []
            price = float(timeslice["price_per_kbps_s"])
            for tenant in sorted(aggregates):
                slot = aggregates[tenant]
                details = []
                for reservation_id in sorted(slot["reservations"]):
                    item = slot["reservations"][reservation_id]
                    details.append({**item, "kbps_seconds": item["kbps"] * item["seconds"]})
                amount = round(slot["kbps_seconds"] * price, 6)
                bill = repository.create_bill(timeslice_id=timeslice["id"], tenant=tenant, kbps_seconds=slot["kbps_seconds"], amount=amount, details=details, now=now)
                bills.append(self._parse_bill(bill))
            repository.set_timeslice_status(timeslice["id"], "settled", now)
            self._record(repository, decision="settle", actor=actor, reservation_id=None, tenant="", timeslice_id=timeslice["id"], reason=f"结算完成，涉及 {len(bills)} 个租户")
            return {"timeslice": dict(repository.timeslice_by_id(timeslice["id"])), "bills": bills}

    # ---------- 查询 ----------

    def list_reservations(self, *, tenant: str | None = None, status: str | None = None, timeslice_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self.repository.list_reservations(tenant=tenant, status=status, timeslice_id=timeslice_id, limit=max(1, min(limit, 500)))

    def get_reservation(self, reservation_id: int) -> dict[str, Any]:
        row = self.repository.reservation_by_id(reservation_id)
        if row is None:
            raise NotFoundError("链路预留不存在")
        result = dict(row)
        result["decisions"] = [self._parse_decision(item) for item in self.repository.decisions_for_reservation(reservation_id)]
        result["ledger"] = self.repository.ledger_for_reservation(reservation_id)
        return result

    def list_decisions(self, *, timeslice_id: int | None = None, decision: str | None = None, tenant: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.repository.list_decisions(timeslice_id=timeslice_id, decision=decision, tenant=tenant, limit=max(1, min(limit, 1000)))
        return [self._parse_decision(row) for row in rows]

    def list_bills(self, *, tenant: str | None = None, timeslice_id: int | None = None) -> list[dict[str, Any]]:
        return [self._parse_bill(row) for row in self.repository.list_bills(tenant=tenant, timeslice_id=timeslice_id)]

    # ---------- 内部规则 ----------

    def _quota_rejection(self, repository: LinkRepository, timeslice_id: int, tenant: str, kbps: int) -> str | None:
        quota = repository.quota(tenant)
        if quota is None:
            return None
        used_kbps, used_count = repository.tenant_usage(timeslice_id, tenant)
        if used_count + 1 > int(quota["max_active_reservations"]):
            return f"租户活动预留数超出配额：当前 {used_count} 条，上限 {quota['max_active_reservations']} 条"
        if used_kbps + kbps > int(quota["max_kbps_per_slice"]):
            return f"租户带宽配额不足：已占用 {used_kbps} kbps，请求 {kbps} kbps，上限 {quota['max_kbps_per_slice']} kbps"
        return None

    def _placement_rejection(self, repository: LinkRepository, timeslice: sqlite3.Row, tenant: str, kbps: int) -> str | None:
        rejection = self._quota_rejection(repository, timeslice["id"], tenant, kbps)
        if rejection is not None:
            return rejection
        available = timeslice["total_kbps"] - repository.reserved_kbps(timeslice["id"])
        if available < kbps:
            return f"时间片可用带宽不足：剩余 {available} kbps，请求 {kbps} kbps"
        return None

    def _auto_preempt(self, repository: LinkRepository, timeslice: sqlite3.Row, priority: sqlite3.Row, payload: dict[str, Any], now: str) -> str | None:
        """紧急消息自动抢占：先模拟可释放总量，足够才实际执行，返回拒绝原因或 None。"""
        available = timeslice["total_kbps"] - repository.reserved_kbps(timeslice["id"])
        if not priority["may_preempt"]:
            return f"时间片可用带宽不足：剩余 {available} kbps，请求 {payload['kbps']} kbps"
        candidates = repository.preemption_candidates(timeslice["id"], priority["rank"])
        releasable = sum(int(item["kbps"]) for item in candidates)
        if available + releasable < payload["kbps"]:
            return f"容量不足，抢占全部低优先级预留后仍无法满足：剩余 {available} kbps，可释放 {releasable} kbps，请求 {payload['kbps']} kbps"
        for candidate in candidates:
            if available >= payload["kbps"]:
                break
            reason = f"被高优先级消息 {payload['message_key']}（{payload['priority_class']}）自动抢占"
            repository.close_ledger(candidate["id"], now, "preempt")
            repository.set_reservation_status(candidate["id"], status="preempted", reason=reason, now=now)
            available += int(candidate["kbps"])
            self._record(repository, decision="preempt", actor=payload["tenant"], reservation_id=candidate["id"], tenant=candidate["tenant"], timeslice_id=timeslice["id"], reason=reason)
        return None

    def _snapshot(self, repository: LinkRepository, timeslice_id: int) -> dict[str, Any]:
        row = repository.timeslice_by_id(timeslice_id)
        reserved = repository.reserved_kbps(timeslice_id)
        return {
            "timeslice_id": row["id"],
            "code": row["code"],
            "status": row["status"],
            "starts_at": row["starts_at"],
            "ends_at": row["ends_at"],
            "total_kbps": row["total_kbps"],
            "reserved_kbps": reserved,
            "available_kbps": row["total_kbps"] - reserved,
            "active_reservations": repository.active_count(timeslice_id),
            "by_tenant": repository.usage_by_tenant(timeslice_id),
            "by_priority_class": repository.usage_by_priority(timeslice_id),
        }

    def _record(self, repository: LinkRepository, *, decision: str, actor: str, reservation_id: int | None, tenant: str, timeslice_id: int | None, reason: str) -> None:
        snapshot = self._snapshot(repository, timeslice_id) if timeslice_id is not None else {}
        repository.add_decision(
            decision=decision, actor=actor, reservation_id=reservation_id, tenant=tenant,
            timeslice_id=timeslice_id, reason=reason, snapshot=snapshot, now=to_storage(self.clock.now()),
        )

    @staticmethod
    def _parse_decision(row: dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["snapshot"] = json.loads(item.pop("snapshot_json"))
        return item

    @staticmethod
    def _parse_bill(row: dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["details"] = json.loads(item.pop("details_json"))
        return item

    @staticmethod
    def _normalize(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
