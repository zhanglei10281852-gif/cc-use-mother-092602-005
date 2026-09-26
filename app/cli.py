from __future__ import annotations

import argparse
import json
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


def command_downlink_demo() -> int:
    now = datetime.now(UTC)

    def window(code: str, start_minutes: int, end_minutes: int, mbps: int) -> dict:
        return {
            "code": code,
            "starts_at": (now + timedelta(minutes=start_minutes)).isoformat(),
            "ends_at": (now + timedelta(minutes=end_minutes)).isoformat(),
            "total_mbps": mbps,
        }

    with TestClient(app) as client:
        for payload in (window("demo-current", -30, 30, 100), window("demo-next", 40, 100, 100), window("demo-past", -120, -60, 50)):
            created = client.post("/api/downlink/slices?actor=cli-demo", json=payload)
            if created.status_code not in {201, 409}:
                print(created.text)
                return 1
        for tenant, price in (("sat-alpha", 1.5), ("sat-beta", 1.0)):
            quota = client.put(f"/api/downlink/tenants/{tenant}/quota?actor=cli-demo", json={"quota_mb": 5000, "price_per_mb": price})
            if quota.status_code != 200:
                print(quota.text)
                return 1
        normal = client.post(
            "/api/downlink/reservations",
            json={"tenant": "sat-beta", "message_key": "demo-normal-1", "size_mb": 200, "priority": 20, "required_mbps": 80},
        )
        emergency = client.post(
            "/api/downlink/reservations",
            json={"tenant": "sat-alpha", "message_key": "demo-emergency-1", "size_mb": 100, "priority": 95, "required_mbps": 50},
        )
        migration = client.post("/api/downlink/migrations", json={"actor": "cli-demo"})
        slices = {item["code"]: item for item in client.get("/api/downlink/slices").json()["items"]}
        settled = client.post("/api/downlink/settlements", json={"slice_id": slices["demo-past"]["id"], "actor": "cli-demo"})
        preemptions = client.get("/api/downlink/decisions", params={"decision": "preempt"})
        summary = client.get("/api/downlink/summary")
    result = {
        "normal": normal.json().get("outcome"),
        "emergency": emergency.json().get("outcome"),
        "preempted": emergency.json().get("preempted"),
        "migration": migration.json(),
        "settle_status": settled.status_code,
        "preempt_decisions": len(preemptions.json()["items"]),
        "summary": summary.json(),
    }
    print(json.dumps(result, ensure_ascii=False))
    ok = (
        normal.status_code == 200
        and emergency.status_code == 200
        and emergency.json().get("outcome") in {"reserved", "replayed"}
        and settled.status_code in {200, 409}
    )
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="compute-operations", description="科学计算任务运营服务维护入口")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化 SQLite 数据库")
    subparsers.add_parser("check-db", help="检查数据库完整性")
    subparsers.add_parser("smoke", help="执行本地 API 冒烟检查")
    subparsers.add_parser("compute-demo", help="执行计算任务提交与领取演示")
    subparsers.add_parser("downlink-demo", help="执行卫星地面链路预留、抢占、迁移与结算演示")
    args = parser.parse_args()
    commands = {
        "init-db": command_init,
        "check-db": command_check,
        "smoke": command_smoke,
        "compute-demo": command_compute_demo,
        "downlink-demo": command_downlink_demo,
    }
    return commands[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
