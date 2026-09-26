from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.downlink.repository import DownlinkRepository

# 优先级达到该值的消息视为紧急遥测，可以抢占普通数据的链路带宽。
EMERGENCY_PRIORITY = 90

SCHEMA = """
CREATE TABLE IF NOT EXISTS downlink_slices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    total_mbps INTEGER NOT NULL CHECK(total_mbps > 0),
    status TEXT NOT NULL DEFAULT 'scheduled' CHECK(status IN ('scheduled','settled')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    settled_at TEXT,
    CHECK(ends_at > starts_at)
);
CREATE TABLE IF NOT EXISTS downlink_quotas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant TEXT NOT NULL UNIQUE,
    quota_mb INTEGER NOT NULL CHECK(quota_mb >= 0),
    price_per_mb REAL NOT NULL DEFAULT 1.0 CHECK(price_per_mb >= 0),
    updated_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS downlink_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant TEXT NOT NULL,
    message_key TEXT NOT NULL,
    size_mb INTEGER NOT NULL CHECK(size_mb > 0),
    priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 100),
    required_mbps INTEGER,
    status TEXT NOT NULL CHECK(status IN ('reserved','deferred','preempted','completed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tenant, message_key)
);
CREATE INDEX IF NOT EXISTS idx_downlink_messages_status ON downlink_messages(status, priority DESC, created_at);
CREATE TABLE IF NOT EXISTS downlink_reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES downlink_messages(id) ON DELETE CASCADE,
    slice_id INTEGER NOT NULL REFERENCES downlink_slices(id) ON DELETE RESTRICT,
    mbps INTEGER NOT NULL CHECK(mbps > 0),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','released','preempted','deferred','migrated','billed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_downlink_reservations_slice ON downlink_reservations(slice_id, status);
CREATE INDEX IF NOT EXISTS idx_downlink_reservations_message ON downlink_reservations(message_id, status);
CREATE TABLE IF NOT EXISTS downlink_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision TEXT NOT NULL CHECK(decision IN ('reserve','reject','defer','preempt','release','migrate','settle')),
    tenant TEXT NOT NULL DEFAULT '',
    message_id INTEGER,
    reservation_id INTEGER,
    slice_id INTEGER,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_downlink_decisions_message ON downlink_decisions(message_id, id);
CREATE INDEX IF NOT EXISTS idx_downlink_decisions_slice ON downlink_decisions(slice_id, id);
CREATE INDEX IF NOT EXISTS idx_downlink_decisions_kind ON downlink_decisions(decision, id);
CREATE TABLE IF NOT EXISTS downlink_bills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slice_id INTEGER NOT NULL REFERENCES downlink_slices(id) ON DELETE RESTRICT,
    tenant TEXT NOT NULL,
    message_id INTEGER NOT NULL REFERENCES downlink_messages(id) ON DELETE RESTRICT,
    reservation_id INTEGER NOT NULL REFERENCES downlink_reservations(id) ON DELETE RESTRICT,
    size_mb INTEGER NOT NULL,
    price_per_mb REAL NOT NULL,
    amount REAL NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(slice_id, reservation_id)
);
CREATE INDEX IF NOT EXISTS idx_downlink_bills_tenant ON downlink_bills(tenant, slice_id);
"""


def ensure_schema() -> None:
    with transaction(immediate=True) as connection:
        connection.executescript(SCHEMA)


class DownlinkService:
    """协调卫星地面链路的时间片、租户配额、消息预留、抢占、迁移与结算。

    明确规则：
    - 预留幂等：同一租户下 message_key 唯一，重试返回首次决策结果，不重复扣减带宽与配额。
    - 配额底线：任何优先级（含紧急遥测）都不得造成租户配额透支，超限一律拒绝。
    - 紧急抢占：紧急消息带宽不足时，按优先级从低到高、同级按预留从晚到早抢占普通消息。
    - 跨片迁移：延期或被抢占的消息按优先级从高到低、同级按提交从早到晚，迁入同时满足
      剩余带宽与租户剩余配额的最早时间片；无法迁入的保持等待并记录原因。
    - 结算计费：仅对传输完成（已释放）的预留按 消息体积 × 租户单价 计费；被抢占、延期、
      迁移的预留不计费；同一时间片只能结算一次，账单唯一约束保证不会重复扣减。
    """

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = DownlinkRepository(self.connection)

    # ---- 登记：时间片与租户配额 ----

    def register_slice(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        starts_at = to_storage(payload["starts_at"])
        ends_at = to_storage(payload["ends_at"])
        with transaction(immediate=True) as connection:
            repository = DownlinkRepository(connection)
            if repository.slice_by_code(payload["code"]):
                raise ConflictError("时间片编码已存在")
            overlap = repository.overlapping_slice(starts_at, ends_at)
            if overlap is not None:
                raise ConflictError("时间片与现有时间片重叠", context={"conflict_with": overlap["code"]})
            slice_row = repository.create_slice(
                code=payload["code"], starts_at=starts_at, ends_at=ends_at,
                total_mbps=payload["total_mbps"], created_by=actor, now=now,
            )
        # 新时间片登记后立即按迁移规则安置等待中的传输。
        migration = self.migrate(actor)
        return {"slice": slice_row, "migration": migration}

    def list_slices(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for row in self.repository.list_slices():
            item = dict(row)
            used = self.repository.slice_used_mbps(row["id"])
            item["used_mbps"] = used
            item["available_mbps"] = int(row["total_mbps"]) - used
            items.append(item)
        return items

    def set_quota(self, tenant: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            return DownlinkRepository(connection).upsert_quota(
                tenant=tenant, quota_mb=payload["quota_mb"], price_per_mb=payload["price_per_mb"], actor=actor, now=now,
            )

    def get_tenant(self, tenant: str) -> dict[str, Any]:
        quota = self.repository.quota(tenant)
        if quota is None:
            raise NotFoundError("租户未登记配额")
        usage = []
        for row in self.repository.list_slices():
            used_mb = self.repository.tenant_used_mb(row["id"], tenant)
            if used_mb:
                usage.append({"slice_id": row["id"], "code": row["code"], "used_mb": used_mb})
        return {"quota": dict(quota), "usage": usage}

    # ---- 预留（幂等） ----

    def reserve(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = DownlinkRepository(connection)
            existing = repository.message_by_key(payload["tenant"], payload["message_key"])
            if existing is not None:
                if (
                    existing["size_mb"] != payload["size_mb"]
                    or existing["priority"] != payload["priority"]
                    or existing["required_mbps"] != payload.get("required_mbps")
                ):
                    raise ConflictError("同一消息键对应了不同的消息参数")
                active = repository.active_reservation_of_message(existing["id"])
                return {
                    "outcome": "replayed",
                    "message": dict(existing),
                    "reservation": dict(active) if active else None,
                    "preempted": [],
                    "reason": "",
                }
            tenant = payload["tenant"]
            quota = repository.quota(tenant)
            if quota is None:
                return self._reject(connection, payload, "租户未登记配额", None, now)
            slice_row = self._target_slice(repository, payload, now)
            if isinstance(slice_row, dict):
                return slice_row  # 目标时间片不可用，已记录拒绝决策
            if slice_row is None:
                return self._defer_new_message(connection, payload, "当前无可用时间片，等待迁移", None, now)
            minimum = self._needed_mbps(payload["size_mb"], slice_row)
            required = payload.get("required_mbps")
            if required is not None and required < minimum:
                raise ValidationError("预留带宽低于消息在时间片内完成传输的最小值", context={"minimum_mbps": minimum})
            if required is not None and required > int(slice_row["total_mbps"]):
                raise ValidationError("预留带宽超出时间片总带宽")
            mbps = required if required is not None else minimum
            if minimum > int(slice_row["total_mbps"]):
                return self._reject(connection, payload, "消息体积超出时间片传输能力", slice_row, now)
            used_mb = repository.tenant_used_mb(slice_row["id"], tenant)
            if used_mb + payload["size_mb"] > int(quota["quota_mb"]):
                return self._reject(connection, payload, "租户配额不足，禁止透支", slice_row, now)
            preempted: list[int] = []
            if repository.slice_used_mbps(slice_row["id"]) + mbps > int(slice_row["total_mbps"]):
                if payload["priority"] >= EMERGENCY_PRIORITY:
                    for candidate in repository.preemption_candidates(slice_row["id"], payload["priority"]):
                        if repository.slice_used_mbps(slice_row["id"]) + mbps <= int(slice_row["total_mbps"]):
                            break
                        self._preempt_reservation(connection, candidate, tenant, "紧急遥测抢占普通数据", now)
                        preempted.append(int(candidate["message_id"]))
                if repository.slice_used_mbps(slice_row["id"]) + mbps > int(slice_row["total_mbps"]):
                    if payload["priority"] >= EMERGENCY_PRIORITY:
                        return self._reject(connection, payload, "紧急抢占后链路带宽仍不足", slice_row, now)
                    return self._defer_new_message(connection, payload, "当前时间片带宽不足，等待迁移", slice_row, now)
            message = repository.create_message(
                tenant=tenant, message_key=payload["message_key"], size_mb=payload["size_mb"],
                priority=payload["priority"], required_mbps=required, status="reserved", now=now,
            )
            reservation = repository.create_reservation(message_id=message["id"], slice_id=slice_row["id"], mbps=mbps, now=now)
            self._record(
                connection, decision="reserve", tenant=tenant, actor=tenant,
                reason="预留成功", slice_row=slice_row, message=repository.message_by_id(message["id"]),
                reservation_id=reservation["id"], now=now,
            )
            return {"outcome": "reserved", "message": message, "reservation": reservation, "preempted": preempted, "reason": ""}

    # ---- 释放 / 抢占 / 延期 ----

    def release(self, reservation_id: int, actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = DownlinkRepository(connection)
            reservation = repository.reservation_by_id(reservation_id)
            if reservation is None:
                raise NotFoundError("链路预留不存在")
            if reservation["status"] != "active":
                raise ConflictError("当前预留状态不允许释放")
            message = repository.message_by_id(reservation["message_id"])
            repository.set_reservation_status(reservation_id, "released", now)
            repository.set_message_status(message["id"], "completed", now)
            self._record(
                connection, decision="release", tenant=message["tenant"], actor=actor,
                reason="传输完成释放带宽", slice_row=repository.slice_by_id(reservation["slice_id"]),
                message=repository.message_by_id(message["id"]), reservation_id=reservation_id, now=now,
            )
            return {
                "reservation": dict(repository.reservation_by_id(reservation_id)),
                "message": dict(repository.message_by_id(message["id"])),
            }

    def preempt(self, reservation_id: int, actor: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = DownlinkRepository(connection)
            reservation = repository.reservation_by_id(reservation_id)
            if reservation is None:
                raise NotFoundError("链路预留不存在")
            if reservation["status"] != "active":
                raise ConflictError("当前预留状态不允许抢占")
            preempted = self._preempt_reservation(connection, reservation, actor, reason, now)
            return preempted

    def defer(self, message_id: int, actor: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = DownlinkRepository(connection)
            message = repository.message_by_id(message_id)
            if message is None:
                raise NotFoundError("链路消息不存在")
            if message["status"] != "reserved":
                raise ConflictError("当前消息状态不允许延期")
            reservation = repository.active_reservation_of_message(message_id)
            repository.set_reservation_status(reservation["id"], "deferred", now)
            repository.set_message_status(message_id, "deferred", now)
            self._record(
                connection, decision="defer", tenant=message["tenant"], actor=actor,
                reason=reason, slice_row=repository.slice_by_id(reservation["slice_id"]),
                message=repository.message_by_id(message_id), reservation_id=reservation["id"], now=now,
            )
            return {
                "message": dict(repository.message_by_id(message_id)),
                "reservation": dict(repository.reservation_by_id(reservation["id"])),
            }

    # ---- 跨时间片迁移 ----

    def migrate(self, actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        migrated: list[dict[str, Any]] = []
        waiting: list[dict[str, Any]] = []
        with transaction(immediate=True) as connection:
            repository = DownlinkRepository(connection)
            slices = repository.migration_slices(now)
            for message in repository.unfinished_messages():
                placed: dict[str, Any] | None = None
                failure = "无满足带宽与配额的时间片"
                for slice_row in slices:
                    mbps = self._effective_mbps(message, slice_row)
                    if mbps > int(slice_row["total_mbps"]):
                        failure = "消息体积超出时间片传输能力"
                        continue
                    if repository.slice_used_mbps(slice_row["id"]) + mbps > int(slice_row["total_mbps"]):
                        failure = "时间片剩余带宽不足"
                        continue
                    quota = repository.quota(message["tenant"])
                    if quota is None:
                        failure = "租户未登记配额"
                        continue
                    if repository.tenant_used_mb(slice_row["id"], message["tenant"]) + int(message["size_mb"]) > int(quota["quota_mb"]):
                        failure = "租户配额不足"
                        continue
                    reservation = repository.create_reservation(message_id=message["id"], slice_id=slice_row["id"], mbps=mbps, now=now)
                    repository.set_message_status(message["id"], "reserved", now)
                    self._record(
                        connection, decision="migrate", tenant=message["tenant"], actor=actor,
                        reason=f"迁移至时间片 {slice_row['code']}", slice_row=slice_row,
                        message=repository.message_by_id(message["id"]), reservation_id=reservation["id"], now=now,
                    )
                    placed = {"message_id": message["id"], "slice_id": slice_row["id"], "reservation_id": reservation["id"]}
                    break
                if placed is not None:
                    migrated.append(placed)
                else:
                    waiting.append({"message_id": message["id"], "reason": failure})
                    self._record(
                        connection, decision="migrate", tenant=message["tenant"], actor=actor,
                        reason=f"迁移等待：{failure}", slice_row=None, message=message, reservation_id=None, now=now,
                    )
        return {"migrated": migrated, "waiting": waiting}

    # ---- 结算 ----

    def settle(self, slice_id: int, actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = DownlinkRepository(connection)
            slice_row = repository.slice_by_id(slice_id)
            if slice_row is None:
                raise NotFoundError("链路时间片不存在")
            if slice_row["status"] != "scheduled":
                raise ConflictError("时间片已完成结算，不能重复结算")
            if slice_row["ends_at"] > now:
                raise ConflictError("时间片尚未结束，不能结算")
            migrated_out: list[int] = []
            for reservation in repository.active_reservations_in_slice(slice_id):
                repository.set_reservation_status(reservation["id"], "migrated", now)
                repository.set_message_status(reservation["message_id"], "deferred", now)
                message = repository.message_by_id(reservation["message_id"])
                self._record(
                    connection, decision="migrate", tenant=message["tenant"], actor=actor,
                    reason="时间片结束传输未完成，转入迁移等待", slice_row=slice_row,
                    message=repository.message_by_id(message["id"]), reservation_id=reservation["id"], now=now,
                )
                migrated_out.append(int(reservation["message_id"]))
            items: list[dict[str, Any]] = []
            totals: dict[str, float] = {}
            for reservation in repository.billable_reservations(slice_id):
                message = repository.message_by_id(reservation["message_id"])
                quota = repository.quota(message["tenant"])
                price = float(quota["price_per_mb"]) if quota is not None else 1.0
                amount = round(int(message["size_mb"]) * price, 4)
                bill = repository.create_bill(
                    slice_id=slice_id, tenant=message["tenant"], message_id=message["id"],
                    reservation_id=reservation["id"], size_mb=message["size_mb"],
                    price_per_mb=price, amount=amount, now=now,
                )
                repository.set_reservation_status(reservation["id"], "billed", now)
                items.append(bill)
                totals[message["tenant"]] = round(totals.get(message["tenant"], 0.0) + amount, 4)
            repository.mark_slice_settled(slice_id, now)
            self._record(
                connection, decision="settle", tenant="", actor=actor,
                reason="时间片结算", slice_row=repository.slice_by_id(slice_id),
                message=None, reservation_id=None, now=now,
            )
            return {
                "slice": dict(repository.slice_by_id(slice_id)),
                "items": items,
                "totals": totals,
                "migrated_out": migrated_out,
            }

    # ---- 管理视图 ----

    def list_messages(self, *, status: str | None = None, tenant: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self.repository.list_messages(status=status, tenant=tenant, limit=max(1, min(limit, 500)))

    def get_message(self, message_id: int) -> dict[str, Any]:
        message = self.repository.message_by_id(message_id)
        if message is None:
            raise NotFoundError("链路消息不存在")
        return {
            "message": dict(message),
            "reservations": self.repository.reservations_of_message(message_id),
            "decisions": self._parse_decisions(self.repository.decisions_of_message(message_id)),
        }

    def list_decisions(self, *, decision: str | None = None, tenant: str | None = None, message_id: int | None = None, slice_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.repository.list_decisions(
            decision=decision, tenant=tenant, message_id=message_id, slice_id=slice_id, limit=max(1, min(limit, 500)),
        )
        return self._parse_decisions(rows)

    def list_bills(self, *, tenant: str | None = None, slice_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self.repository.list_bills(tenant=tenant, slice_id=slice_id, limit=max(1, min(limit, 500)))

    def bills_summary(self) -> list[dict[str, Any]]:
        return self.repository.bills_summary()

    def summary(self) -> dict[str, Any]:
        bill_count, bill_total = self.repository.count_bills()
        slices = self.repository.count_slices()
        return {
            "messages": self.repository.count_messages_by_status(),
            "slices": {"total": sum(slices.values()), **slices},
            "bills": {"count": bill_count, "total_amount": round(bill_total, 4)},
            "decisions": self.repository.count_decisions(),
        }

    # ---- 内部 helpers ----

    def _target_slice(self, repository: DownlinkRepository, payload: dict[str, Any], now: str) -> sqlite3.Row | dict[str, Any] | None:
        """返回目标时间片；返回 dict 表示已生成拒绝结果，返回 None 表示当前无可用时间片。"""
        slice_id = payload.get("slice_id")
        if slice_id is None:
            return repository.current_slice(now)
        slice_row = repository.slice_by_id(slice_id)
        if slice_row is None:
            raise NotFoundError("链路时间片不存在")
        if slice_row["status"] != "scheduled" or slice_row["ends_at"] <= now:
            return self._reject(repository.connection, payload, "目标时间片不可用", slice_row, now)
        return slice_row

    def _reject(self, connection: sqlite3.Connection, payload: dict[str, Any], reason: str, slice_row: sqlite3.Row | None, now: str) -> dict[str, Any]:
        """拒绝不落消息记录，重试在条件满足后可以重新进入；决策日志保留原因与快照。"""
        snapshot_message = {
            "tenant": payload["tenant"], "message_key": payload["message_key"],
            "size_mb": payload["size_mb"], "priority": payload["priority"], "status": "rejected",
        }
        self._record(
            connection, decision="reject", tenant=payload["tenant"], actor=payload["tenant"],
            reason=reason, slice_row=slice_row, message=snapshot_message, reservation_id=None, now=now,
        )
        return {
            "outcome": "rejected",
            "message": {**snapshot_message, "reject_reason": reason},
            "reservation": None,
            "preempted": [],
            "reason": reason,
        }

    def _defer_new_message(self, connection: sqlite3.Connection, payload: dict[str, Any], reason: str, slice_row: sqlite3.Row | None, now: str) -> dict[str, Any]:
        repository = DownlinkRepository(connection)
        message = repository.create_message(
            tenant=payload["tenant"], message_key=payload["message_key"], size_mb=payload["size_mb"],
            priority=payload["priority"], required_mbps=payload.get("required_mbps"), status="deferred", now=now,
        )
        self._record(
            connection, decision="defer", tenant=payload["tenant"], actor=payload["tenant"],
            reason=reason, slice_row=slice_row, message=message, reservation_id=None, now=now,
        )
        return {"outcome": "deferred", "message": message, "reservation": None, "preempted": [], "reason": reason}

    def _preempt_reservation(self, connection: sqlite3.Connection, reservation: sqlite3.Row, actor: str, reason: str, now: str) -> dict[str, Any]:
        repository = DownlinkRepository(connection)
        repository.set_reservation_status(reservation["id"], "preempted", now)
        repository.set_message_status(reservation["message_id"], "preempted", now)
        message = repository.message_by_id(reservation["message_id"])
        self._record(
            connection, decision="preempt", tenant=message["tenant"], actor=actor,
            reason=reason, slice_row=repository.slice_by_id(reservation["slice_id"]),
            message=message, reservation_id=reservation["id"], now=now,
        )
        return {
            "reservation": dict(repository.reservation_by_id(reservation["id"])),
            "message": dict(repository.message_by_id(message["id"])),
        }

    def _record(
        self,
        connection: sqlite3.Connection,
        *,
        decision: str,
        tenant: str,
        actor: str,
        reason: str,
        slice_row: sqlite3.Row | None,
        message: sqlite3.Row | dict[str, Any] | None,
        reservation_id: int | None,
        now: str,
    ) -> None:
        repository = DownlinkRepository(connection)
        snapshot = self._snapshot(repository, slice_row=slice_row, tenant=tenant or None, message=message, now=now)
        message_id = None
        if message is not None:
            message_data = dict(message)
            if message_data.get("id") is not None:
                message_id = int(message_data["id"])
        repository.add_decision(
            decision=decision, tenant=tenant, actor=actor, reason=reason,
            message_id=message_id, reservation_id=reservation_id,
            slice_id=slice_row["id"] if slice_row is not None else None,
            snapshot=snapshot, now=now,
        )

    def _snapshot(self, repository: DownlinkRepository, *, slice_row: sqlite3.Row | None, tenant: str | None, message: sqlite3.Row | dict[str, Any] | None, now: str) -> dict[str, Any]:
        """采集决策生效时刻的链路带宽与租户用量快照。"""
        snapshot: dict[str, Any] = {"captured_at": now}
        if slice_row is not None:
            used = repository.slice_used_mbps(slice_row["id"])
            snapshot["slice"] = {
                "id": slice_row["id"],
                "code": slice_row["code"],
                "starts_at": slice_row["starts_at"],
                "ends_at": slice_row["ends_at"],
                "total_mbps": int(slice_row["total_mbps"]),
                "used_mbps": used,
                "available_mbps": int(slice_row["total_mbps"]) - used,
                "status": slice_row["status"],
            }
        if tenant:
            quota = repository.quota(tenant)
            used_mb = repository.tenant_used_mb(slice_row["id"], tenant) if slice_row is not None else 0
            snapshot["tenant"] = {
                "tenant": tenant,
                "quota_mb": int(quota["quota_mb"]) if quota is not None else None,
                "used_mb": used_mb,
                "remaining_mb": (int(quota["quota_mb"]) - used_mb) if quota is not None else None,
            }
        if message is not None:
            data = dict(message)
            snapshot["message"] = {
                "id": data.get("id"),
                "tenant": data.get("tenant"),
                "message_key": data.get("message_key"),
                "size_mb": data.get("size_mb"),
                "priority": data.get("priority"),
                "status": data.get("status"),
            }
        return snapshot

    @staticmethod
    def _needed_mbps(size_mb: int, slice_row: sqlite3.Row) -> int:
        """消息在时间片内完成传输所需的最小带宽（Mbps）。"""
        start = from_storage(slice_row["starts_at"])
        end = from_storage(slice_row["ends_at"])
        seconds = max(1, int((end - start).total_seconds()))
        return max(1, -(-int(size_mb) * 8 // seconds))

    def _effective_mbps(self, message: sqlite3.Row, slice_row: sqlite3.Row) -> int:
        """迁移时取消息声明带宽与目标时间片最小需求的较大者。"""
        minimum = self._needed_mbps(message["size_mb"], slice_row)
        required = message["required_mbps"] or 0
        return max(minimum, int(required))

    @staticmethod
    def _parse_decisions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items = []
        for row in rows:
            item = dict(row)
            item["snapshot"] = json.loads(item.pop("snapshot_json"))
            items.append(item)
        return items
