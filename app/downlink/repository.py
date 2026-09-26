from __future__ import annotations

import json
import sqlite3
from typing import Any


class DownlinkRepository:
    """封装卫星地面链路协调领域的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ---- 链路时间片 ----

    def slice_by_id(self, slice_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM downlink_slices WHERE id=?", (slice_id,)).fetchone()

    def slice_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM downlink_slices WHERE code=?", (code,)).fetchone()

    def overlapping_slice(self, starts_at: str, ends_at: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM downlink_slices WHERE starts_at<? AND ends_at>? ORDER BY starts_at LIMIT 1",
            (ends_at, starts_at),
        ).fetchone()

    def create_slice(self, *, code: str, starts_at: str, ends_at: str, total_mbps: int, created_by: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO downlink_slices(code,starts_at,ends_at,total_mbps,status,created_by,created_at) VALUES(?,?,?,?,'scheduled',?,?)",
            (code, starts_at, ends_at, total_mbps, created_by, now),
        )
        return dict(self.slice_by_id(cursor.lastrowid))

    def list_slices(self) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM downlink_slices ORDER BY starts_at,id").fetchall()

    def current_slice(self, now: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM downlink_slices WHERE status='scheduled' AND starts_at<=? AND ends_at>? ORDER BY starts_at LIMIT 1",
            (now, now),
        ).fetchone()

    def migration_slices(self, now: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM downlink_slices WHERE status='scheduled' AND ends_at>? ORDER BY starts_at,id",
            (now,),
        ).fetchall()

    def mark_slice_settled(self, slice_id: int, now: str) -> None:
        self.connection.execute("UPDATE downlink_slices SET status='settled',settled_at=? WHERE id=?", (now, slice_id))

    def count_slices(self) -> dict[str, int]:
        rows = self.connection.execute("SELECT status,COUNT(*) AS amount FROM downlink_slices GROUP BY status").fetchall()
        return {str(row["status"]): int(row["amount"]) for row in rows}

    # ---- 租户配额 ----

    def quota(self, tenant: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM downlink_quotas WHERE tenant=?", (tenant,)).fetchone()

    def upsert_quota(self, *, tenant: str, quota_mb: int, price_per_mb: float, actor: str, now: str) -> dict[str, Any]:
        self.connection.execute(
            "INSERT INTO downlink_quotas(tenant,quota_mb,price_per_mb,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(tenant) DO UPDATE SET quota_mb=excluded.quota_mb,price_per_mb=excluded.price_per_mb,"
            "updated_by=excluded.updated_by,updated_at=excluded.updated_at",
            (tenant, quota_mb, price_per_mb, actor, now, now),
        )
        return dict(self.quota(tenant))

    # ---- 消息 ----

    def message_by_id(self, message_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM downlink_messages WHERE id=?", (message_id,)).fetchone()

    def message_by_key(self, tenant: str, message_key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM downlink_messages WHERE tenant=? AND message_key=?",
            (tenant, message_key),
        ).fetchone()

    def create_message(self, *, tenant: str, message_key: str, size_mb: int, priority: int, required_mbps: int | None, status: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO downlink_messages(tenant,message_key,size_mb,priority,required_mbps,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (tenant, message_key, size_mb, priority, required_mbps, status, now, now),
        )
        return dict(self.message_by_id(cursor.lastrowid))

    def set_message_status(self, message_id: int, status: str, now: str) -> None:
        self.connection.execute("UPDATE downlink_messages SET status=?,updated_at=? WHERE id=?", (status, now, message_id))

    def list_messages(self, *, status: str | None, tenant: str | None, limit: int) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if status:
            clauses.append("status=?")
            values.append(status)
        if tenant:
            clauses.append("tenant=?")
            values.append(tenant)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        rows = self.connection.execute("SELECT * FROM downlink_messages" + where + " ORDER BY id DESC LIMIT ?", values).fetchall()
        return [dict(row) for row in rows]

    def unfinished_messages(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM downlink_messages WHERE status IN ('deferred','preempted') ORDER BY priority DESC,created_at ASC,id ASC"
        ).fetchall()

    def count_messages_by_status(self) -> dict[str, int]:
        rows = self.connection.execute("SELECT status,COUNT(*) AS amount FROM downlink_messages GROUP BY status").fetchall()
        return {str(row["status"]): int(row["amount"]) for row in rows}

    # ---- 预留 ----

    def reservation_by_id(self, reservation_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM downlink_reservations WHERE id=?", (reservation_id,)).fetchone()

    def create_reservation(self, *, message_id: int, slice_id: int, mbps: int, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO downlink_reservations(message_id,slice_id,mbps,status,created_at,updated_at) VALUES(?,?,?,'active',?,?)",
            (message_id, slice_id, mbps, now, now),
        )
        return dict(self.reservation_by_id(cursor.lastrowid))

    def set_reservation_status(self, reservation_id: int, status: str, now: str) -> None:
        self.connection.execute("UPDATE downlink_reservations SET status=?,updated_at=? WHERE id=?", (status, now, reservation_id))

    def active_reservation_of_message(self, message_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM downlink_reservations WHERE message_id=? AND status='active' ORDER BY id DESC LIMIT 1",
            (message_id,),
        ).fetchone()

    def reservations_of_message(self, message_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM downlink_reservations WHERE message_id=? ORDER BY id", (message_id,)).fetchall()
        return [dict(row) for row in rows]

    def preemption_candidates(self, slice_id: int, priority: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT r.*,m.priority AS message_priority FROM downlink_reservations r"
            " JOIN downlink_messages m ON m.id=r.message_id"
            " WHERE r.slice_id=? AND r.status='active' AND m.priority<?"
            " ORDER BY m.priority ASC,r.created_at DESC,r.id DESC",
            (slice_id, priority),
        ).fetchall()

    def active_reservations_in_slice(self, slice_id: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM downlink_reservations WHERE slice_id=? AND status='active' ORDER BY id",
            (slice_id,),
        ).fetchall()

    def billable_reservations(self, slice_id: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM downlink_reservations WHERE slice_id=? AND status='released' ORDER BY id",
            (slice_id,),
        ).fetchall()

    def slice_used_mbps(self, slice_id: int) -> int:
        return int(
            self.connection.execute(
                "SELECT COALESCE(SUM(mbps),0) FROM downlink_reservations WHERE slice_id=? AND status='active'",
                (slice_id,),
            ).fetchone()[0]
        )

    def tenant_used_mb(self, slice_id: int, tenant: str) -> int:
        return int(
            self.connection.execute(
                "SELECT COALESCE(SUM(m.size_mb),0) FROM downlink_reservations r JOIN downlink_messages m ON m.id=r.message_id"
                " WHERE r.slice_id=? AND m.tenant=? AND r.status IN ('active','released','billed')",
                (slice_id, tenant),
            ).fetchone()[0]
        )

    # ---- 决策日志（含带宽快照） ----

    def add_decision(self, *, decision: str, tenant: str, actor: str, reason: str, message_id: int | None, reservation_id: int | None, slice_id: int | None, snapshot: dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO downlink_decisions(decision,tenant,actor,reason,message_id,reservation_id,slice_id,snapshot_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (decision, tenant, actor, reason, message_id, reservation_id, slice_id, json.dumps(snapshot, ensure_ascii=False, sort_keys=True), now),
        )

    def list_decisions(self, *, decision: str | None, tenant: str | None, message_id: int | None, slice_id: int | None, limit: int) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if decision:
            clauses.append("decision=?")
            values.append(decision)
        if tenant:
            clauses.append("tenant=?")
            values.append(tenant)
        if message_id is not None:
            clauses.append("message_id=?")
            values.append(message_id)
        if slice_id is not None:
            clauses.append("slice_id=?")
            values.append(slice_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        rows = self.connection.execute("SELECT * FROM downlink_decisions" + where + " ORDER BY id DESC LIMIT ?", values).fetchall()
        return [dict(row) for row in rows]

    def decisions_of_message(self, message_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM downlink_decisions WHERE message_id=? ORDER BY id", (message_id,)).fetchall()
        return [dict(row) for row in rows]

    def count_decisions(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM downlink_decisions").fetchone()[0])

    # ---- 账单 ----

    def create_bill(self, *, slice_id: int, tenant: str, message_id: int, reservation_id: int, size_mb: int, price_per_mb: float, amount: float, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO downlink_bills(slice_id,tenant,message_id,reservation_id,size_mb,price_per_mb,amount,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (slice_id, tenant, message_id, reservation_id, size_mb, price_per_mb, amount, now),
        )
        return dict(self.connection.execute("SELECT * FROM downlink_bills WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_bills(self, *, tenant: str | None, slice_id: int | None, limit: int) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if tenant:
            clauses.append("tenant=?")
            values.append(tenant)
        if slice_id is not None:
            clauses.append("slice_id=?")
            values.append(slice_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        rows = self.connection.execute("SELECT * FROM downlink_bills" + where + " ORDER BY id DESC LIMIT ?", values).fetchall()
        return [dict(row) for row in rows]

    def bills_summary(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT tenant,COUNT(*) AS items,COALESCE(SUM(size_mb),0) AS total_mb,COALESCE(SUM(amount),0) AS total_amount"
            " FROM downlink_bills GROUP BY tenant ORDER BY tenant"
        ).fetchall()
        return [dict(row) for row in rows]

    def count_bills(self) -> tuple[int, float]:
        row = self.connection.execute("SELECT COUNT(*) AS items,COALESCE(SUM(amount),0) AS total FROM downlink_bills").fetchone()
        return int(row["items"]), float(row["total"])
