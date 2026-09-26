from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.database import database_path, get_connection, init_db
from app.main import app


def command_init() -> int:
    init_db()
    print(json.dumps({"database": str(database_path()), "status": "initialized"}, ensure_ascii=False))
    return 0


def command_check() -> int:
    init_db()
    connection = get_connection()
    result = {
        "database": str(database_path()),
        "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
        "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        "tables": connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0],
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["integrity"] == "ok" and result["foreign_keys"] == 1 else 1


def command_smoke() -> int:
    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/api/system/health")
    result = {"root": root.json(), "health": health.json(), "status_codes": [root.status_code, health.status_code]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status_codes"] == [200, 200] else 1


def command_compute_demo() -> int:
    template = {
        "code": "monte-carlo-demo",
        "name": "蒙特卡洛演示",
        "algorithm": "monte-carlo",
        "parameter_schema": {
            "samples": {"type": "integer", "required": True, "minimum": 10, "maximum": 1000000},
            "seed": {"type": "integer", "required": True},
        },
        "default_parameters": {},
        "max_runtime_seconds": 60,
        "max_attempts": 3,
    }
    with TestClient(app) as client:
        created = client.post("/api/compute/templates?actor=cli-demo", json=template)
        if created.status_code not in {201, 409}:
            print(created.text)
            return 1
        task = client.post(
            "/api/compute/tasks",
            json={
                "template_code": "monte-carlo-demo",
                "project_code": "demo",
                "requested_by": "cli-user",
                "parameters": {"samples": 1000, "seed": 42},
                "priority": 80,
                "idempotency_key": "compute-demo-000001",
            },
        )
        claimed = client.post(
            "/api/compute/tasks/claim",
            json={"worker_id": "cli-worker", "capabilities": ["monte-carlo"], "lease_seconds": 60},
        )
    result = {"task": task.status_code, "claimed": claimed.status_code, "task_id": task.json().get("id")}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if task.status_code == 202 and claimed.status_code == 200 and claimed.json().get("task") else 1


def command_link_demo() -> int:
    suffix = str(int(time.time()))
    base = datetime.now(UTC).replace(microsecond=0)
    with TestClient(app) as client:
        for code, rank, may_preempt in (("emergency", 100, True), ("bulk", 10, False)):
            created = client.post(
                "/api/link/priority-classes?actor=cli-demo",
                json={"code": code, "rank": rank, "may_preempt": may_preempt, "description": "演示优先级"},
            )
            if created.status_code not in {201, 409}:
                print(created.text)
                return 1
        client.put("/api/link/quotas?actor=cli-demo", json={"tenant": "sat-demo-u", "max_kbps_per_slice": 800, "max_active_reservations": 10})
        slice_a = client.post(
            "/api/link/timeslices?actor=cli-demo",
            json={"code": f"demo-a-{suffix}", "starts_at": base.isoformat(), "ends_at": (base + timedelta(hours=1)).isoformat(), "total_kbps": 1000, "price_per_kbps_s": 0.5},
        ).json()
        slice_b = client.post(
            "/api/link/timeslices?actor=cli-demo",
            json={"code": f"demo-b-{suffix}", "starts_at": (base + timedelta(hours=1)).isoformat(), "ends_at": (base + timedelta(hours=2)).isoformat(), "total_kbps": 600, "price_per_kbps_s": 1.0},
        ).json()
        bulk = client.post("/api/link/reservations", json={"tenant": "sat-demo-n", "message_key": f"demo-bulk-{suffix}", "priority_class": "bulk", "kbps": 800, "timeslice_id": slice_a["id"]}).json()
        urgent = client.post("/api/link/reservations", json={"tenant": "sat-demo-u", "message_key": f"demo-urgent-{suffix}", "priority_class": "emergency", "kbps": 500, "timeslice_id": slice_a["id"]}).json()
        bulk_after = client.get(f"/api/link/reservations/{bulk['id']}").json()
        rollover = client.post(f"/api/link/timeslices/{slice_a['id']}/rollover", json={"actor": "cli-demo", "target_timeslice_id": slice_b["id"]}).json()
        settled = client.post(f"/api/link/timeslices/{slice_a['id']}/settle", json={"actor": "cli-demo"}).json()
        bills = client.get(f"/api/link/bills?timeslice_id={slice_a['id']}").json()["items"]
    result = {
        "bulk_after_preemption": bulk_after["status"],
        "urgent": urgent["status"],
        "rollover": {"migrated": rollover["migrated"], "deferred": rollover["deferred"], "expired": rollover["expired"]},
        "settled_status": settled["timeslice"]["status"],
        "bills": len(bills),
    }
    print(json.dumps(result, ensure_ascii=False))
    ok = (
        bulk_after["status"] == "preempted"
        and urgent["status"] == "reserved"
        and rollover["migrated"] == [urgent["id"]]
        and settled["timeslice"]["status"] == "settled"
        and len(bills) == 2
    )
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="compute-operations", description="科学计算任务运营服务维护入口")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化 SQLite 数据库")
    subparsers.add_parser("check-db", help="检查数据库完整性")
    subparsers.add_parser("smoke", help="执行本地 API 冒烟检查")
    subparsers.add_parser("compute-demo", help="执行计算任务提交与领取演示")
    subparsers.add_parser("link-demo", help="执行链路预留、抢占、迁移与结算演示")
    args = parser.parse_args()
    commands = {
        "init-db": command_init,
        "check-db": command_check,
        "smoke": command_smoke,
        "compute-demo": command_compute_demo,
        "link-demo": command_link_demo,
    }
    return commands[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
