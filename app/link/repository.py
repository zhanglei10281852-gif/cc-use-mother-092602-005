from __future__ import annotations

import json
import sqlite3
from typing import Any


class LinkRepository:
    """封装卫星链路带宽协调领域的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ---------- 时间片 ----------

    def timeslice_by_id(self, timeslice_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM link_timeslices WHERE id=?", (timeslice_id,)).fetchone()

    def timeslice_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM link_timeslices WHERE code=?", (code,)).fetchone()

    def create_timeslice(self, *, code: str, starts_at: str, ends_at: str, total_kbps: int, price_per_kbps_s: float, created_by: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO link_timeslices(code,starts_at,ends_at,total_kbps,price_per_kbps_s,status,created_by,created_at,updated_at) VALUES(?,?,?,?,?,'open',?,?,?)",
            (code, starts_at, ends_at, total_kbps, price_per_kbps_s, created_by, now, now),
        )
        return dict(self.timeslice_by_id(cursor.lastrowid))

    def list_timeslices(self, status: str | None) -> list[dict[str, Any]]:
        if status:
            rows = self.connection.execute("SELECT * FROM link_timeslices WHERE status=? ORDER BY starts_at,id", (status,)).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM link_timeslices ORDER BY starts_at,id").fetchall()
        return [dict(row) for row in rows]

    def set_timeslice_status(self, timeslice_id: int, status: str, now: str) -> None:
        self.connection.execute("UPDATE link_timeslices SET status=?,updated_at=? WHERE id=?", (status, now, timeslice_id))

    def next_open_timeslice(self, exclude_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM link_timeslices WHERE status='open' AND id<>? ORDER BY starts_at ASC,id ASC LIMIT 1",
            (exclude_id,),
        ).fetchone()

    # ---------- 租户配额 ----------

    def quota(self, tenant: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM link_tenant_quotas WHERE tenant=?", (tenant,)).fetchone()

    def upsert_quota(self, *, tenant: str, max_kbps_per_slice: int, max_active_reservations: int, actor: str, now: str) -> dict[str, Any]:
        self.connection.execute(
            "INSERT INTO link_tenant_quotas(tenant,max_kbps_per_slice,max_active_reservations,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(tenant) DO UPDATE SET max_kbps_per_slice=excluded.max_kbps_per_slice,max_active_reservations=excluded.max_active_reservations,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
            (tenant, max_kbps_per_slice, max_active_reservations, actor, now, now),
        )
        return dict(self.quota(tenant))

    def list_quotas(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM link_tenant_quotas ORDER BY tenant").fetchall()
        return [dict(row) for row in rows]

    # ---------- 优先级类别 ----------

    def priority_class_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM link_priority_classes WHERE code=?", (code,)).fetchone()

    def create_priority_class(self, *, code: str, rank: int, may_preempt: int, description: str, created_by: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO link_priority_classes(code,rank,may_preempt,description,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (code, rank, may_preempt, description, created_by, now, now),
        )
        return dict(self.connection.execute("SELECT * FROM link_priority_classes WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_priority_classes(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM link_priority_classes ORDER BY rank DESC,code").fetchall()
        return [dict(row) for row in rows]

    # ---------- 预留 ----------

    def reservation_by_id(self, reservation_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM link_reservations WHERE id=?", (reservation_id,)).fetchone()

    def reservation_by_key(self, tenant: str, message_key: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM link_reservations WHERE tenant=? AND message_key=?", (tenant, message_key)).fetchone()

    def create_reservation(self, *, tenant: str, message_key: str, payload_digest: str, priority_class: str, priority_rank: int, kbps: int, timeslice_id: int | None, status: str, status_reason: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO link_reservations(tenant,message_key,payload_digest,priority_class,priority_rank,kbps,timeslice_id,status,status_reason,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,1,?,?)",
            (tenant, message_key, payload_digest, priority_class, priority_rank, kbps, timeslice_id, status, status_reason, now, now),
        )
        return dict(self.reservation_by_id(cursor.lastrowid))

    def set_reservation_status(self, reservation_id: int, *, status: str, reason: str, now: str) -> None:
        self.connection.execute(
            "UPDATE link_reservations SET status=?,status_reason=?,updated_at=?,version=version+1 WHERE id=?",
            (status, reason, now, reservation_id),
        )

    def move_reservation(self, reservation_id: int, *, timeslice_id: int | None, status: str, reason: str, now: str) -> None:
        self.connection.execute(
            "UPDATE link_reservations SET timeslice_id=?,status=?,status_reason=?,updated_at=?,version=version+1 WHERE id=?",
            (timeslice_id, status, reason, now, reservation_id),
        )

    def list_reservations(self, *, tenant: str | None, status: str | None, timeslice_id: int | None, limit: int) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if tenant:
            clauses.append("tenant=?")
            values.append(tenant)
        if status:
            clauses.append("status=?")
            values.append(status)
        if timeslice_id is not None:
            clauses.append("timeslice_id=?")
            values.append(timeslice_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        rows = self.connection.execute("SELECT * FROM link_reservations" + where + " ORDER BY id DESC LIMIT ?", values).fetchall()
        return [dict(row) for row in rows]

    def active_reservations(self, timeslice_id: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM link_reservations WHERE timeslice_id=? AND status='reserved' ORDER BY priority_rank DESC,created_at ASC,id ASC",
            (timeslice_id,),
        ).fetchall()

    def preemption_candidates(self, timeslice_id: int, rank: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM link_reservations WHERE timeslice_id=? AND status='reserved' AND priority_rank<? ORDER BY priority_rank ASC,created_at DESC,id DESC",
            (timeslice_id, rank),
        ).fetchall()

    # ---------- 占用统计 ----------

    def reserved_kbps(self, timeslice_id: int) -> int:
        return int(self.connection.execute("SELECT COALESCE(SUM(kbps),0) FROM link_reservations WHERE timeslice_id=? AND status='reserved'", (timeslice_id,)).fetchone()[0])

    def active_count(self, timeslice_id: int) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM link_reservations WHERE timeslice_id=? AND status='reserved'", (timeslice_id,)).fetchone()[0])

    def tenant_usage(self, timeslice_id: int, tenant: str) -> tuple[int, int]:
        row = self.connection.execute(
            "SELECT COALESCE(SUM(kbps),0) AS kbps,COUNT(*) AS amount FROM link_reservations WHERE timeslice_id=? AND tenant=? AND status='reserved'",
            (timeslice_id, tenant),
        ).fetchone()
        return int(row["kbps"]), int(row["amount"])

    def usage_summary(self) -> dict[int, int]:
        rows = self.connection.execute("SELECT timeslice_id,SUM(kbps) AS kbps FROM link_reservations WHERE status='reserved' GROUP BY timeslice_id").fetchall()
        return {int(row["timeslice_id"]): int(row["kbps"]) for row in rows}

    def usage_by_tenant(self, timeslice_id: int) -> dict[str, int]:
        rows = self.connection.execute("SELECT tenant,SUM(kbps) AS kbps FROM link_reservations WHERE timeslice_id=? AND status='reserved' GROUP BY tenant ORDER BY tenant", (timeslice_id,)).fetchall()
        return {str(row["tenant"]): int(row["kbps"]) for row in rows}

    def usage_by_priority(self, timeslice_id: int) -> dict[str, int]:
        rows = self.connection.execute("SELECT priority_class,SUM(kbps) AS kbps FROM link_reservations WHERE timeslice_id=? AND status='reserved' GROUP BY priority_class ORDER BY priority_class", (timeslice_id,)).fetchall()
        return {str(row["priority_class"]): int(row["kbps"]) for row in rows}

    # ---------- 占用流水 ----------

    def open_ledger(self, *, reservation_id: int, tenant: str, timeslice_id: int, kbps: int, now: str) -> None:
        self.connection.execute(
            "INSERT INTO link_usage_ledger(reservation_id,tenant,timeslice_id,kbps,started_at) VALUES(?,?,?,?,?)",
            (reservation_id, tenant, timeslice_id, kbps, now),
        )

    def close_ledger(self, reservation_id: int, now: str, reason: str) -> None:
        self.connection.execute(
            "UPDATE link_usage_ledger SET ended_at=?,end_reason=? WHERE reservation_id=? AND ended_at IS NULL",
            (now, reason, reservation_id),
        )

    def close_open_ledger_entries(self, timeslice_id: int, now: str, reason: str) -> None:
        self.connection.execute(
            "UPDATE link_usage_ledger SET ended_at=?,end_reason=? WHERE timeslice_id=? AND ended_at IS NULL",
            (now, reason, timeslice_id),
        )

    def ledger_entries(self, timeslice_id: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT l.*,r.message_key FROM link_usage_ledger l JOIN link_reservations r ON r.id=l.reservation_id WHERE l.timeslice_id=? ORDER BY l.id",
            (timeslice_id,),
        ).fetchall()

    def ledger_for_reservation(self, reservation_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM link_usage_ledger WHERE reservation_id=? ORDER BY id", (reservation_id,)).fetchall()
        return [dict(row) for row in rows]

    # ---------- 决策日志 ----------

    def add_decision(self, *, decision: str, actor: str, reservation_id: int | None, tenant: str, timeslice_id: int | None, reason: str, snapshot: dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO link_decisions(decision,actor,reservation_id,tenant,timeslice_id,reason,snapshot_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (decision, actor, reservation_id, tenant, timeslice_id, reason, json.dumps(snapshot, ensure_ascii=False, sort_keys=True), now),
        )

    def decisions_for_reservation(self, reservation_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM link_decisions WHERE reservation_id=? ORDER BY id", (reservation_id,)).fetchall()
        return [dict(row) for row in rows]

    def list_decisions(self, *, timeslice_id: int | None, decision: str | None, tenant: str | None, limit: int) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if timeslice_id is not None:
            clauses.append("timeslice_id=?")
            values.append(timeslice_id)
        if decision:
            clauses.append("decision=?")
            values.append(decision)
        if tenant:
            clauses.append("tenant=?")
            values.append(tenant)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        rows = self.connection.execute("SELECT * FROM link_decisions" + where + " ORDER BY id LIMIT ?", values).fetchall()
        return [dict(row) for row in rows]

    # ---------- 账单 ----------

    def create_bill(self, *, timeslice_id: int, tenant: str, kbps_seconds: int, amount: float, details: list[dict[str, Any]], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO link_bills(timeslice_id,tenant,kbps_seconds,amount,details_json,created_at) VALUES(?,?,?,?,?,?)",
            (timeslice_id, tenant, kbps_seconds, amount, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
        )
        return dict(self.connection.execute("SELECT * FROM link_bills WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_bills(self, *, tenant: str | None, timeslice_id: int | None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if tenant:
            clauses.append("tenant=?")
            values.append(tenant)
        if timeslice_id is not None:
            clauses.append("timeslice_id=?")
            values.append(timeslice_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute("SELECT * FROM link_bills" + where + " ORDER BY id", values).fetchall()
        return [dict(row) for row in rows]
