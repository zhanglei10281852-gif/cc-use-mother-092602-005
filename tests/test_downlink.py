from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.clock import FrozenClock
from app.core.errors import ConflictError
from app.database import get_connection
from app.downlink.service import DownlinkService


def slice_payload(code: str, start_minutes: int, end_minutes: int, total_mbps: int = 100) -> dict:
    start = datetime.now(UTC) + timedelta(minutes=start_minutes)
    end = datetime.now(UTC) + timedelta(minutes=end_minutes)
    return {"code": code, "starts_at": start.isoformat(), "ends_at": end.isoformat(), "total_mbps": total_mbps}


def register_slice(client, code: str, start_minutes: int, end_minutes: int, total_mbps: int = 100) -> dict:
    response = client.post("/api/downlink/slices?actor=operator", json=slice_payload(code, start_minutes, end_minutes, total_mbps))
    assert response.status_code == 201, response.text
    return response.json()


def set_quota(client, tenant: str, quota_mb: int, price_per_mb: float = 1.0) -> dict:
    response = client.put(
        f"/api/downlink/tenants/{tenant}/quota?actor=operator",
        json={"quota_mb": quota_mb, "price_per_mb": price_per_mb},
    )
    assert response.status_code == 200, response.text
    return response.json()


def reserve_payload(tenant: str, key: str, size_mb: int, *, priority: int = 50, required_mbps: int | None = None, slice_id: int | None = None) -> dict:
    payload: dict = {"tenant": tenant, "message_key": key, "size_mb": size_mb, "priority": priority}
    if required_mbps is not None:
        payload["required_mbps"] = required_mbps
    if slice_id is not None:
        payload["slice_id"] = slice_id
    return payload


def test_slice_registration_rules(client):
    created = register_slice(client, "window-a", -30, 30, 100)
    assert created["slice"]["status"] == "scheduled"
    assert created["migration"] == {"migrated": [], "waiting": []}

    duplicate = client.post("/api/downlink/slices?actor=operator", json=slice_payload("window-a", 60, 120))
    assert duplicate.status_code == 409

    overlap = client.post("/api/downlink/slices?actor=operator", json=slice_payload("window-b", 0, 60))
    assert overlap.status_code == 409

    adjacent = client.post("/api/downlink/slices?actor=operator", json=slice_payload("window-c", 30, 90))
    assert adjacent.status_code == 201

    invalid = slice_payload("window-d", 10, 5)
    rejected = client.post("/api/downlink/slices?actor=operator", json=invalid)
    assert rejected.status_code == 422

    listing = client.get("/api/downlink/slices")
    assert listing.status_code == 200
    items = {item["code"]: item for item in listing.json()["items"]}
    assert items["window-a"]["used_mbps"] == 0
    assert items["window-a"]["available_mbps"] == 100


def test_reserve_requires_quota_and_retry_never_double_charges(client):
    register_slice(client, "window-quota", -30, 30, 100)
    payload = reserve_payload("sat-x", "msg-0001", 40, required_mbps=10)

    rejected = client.post("/api/downlink/reservations", json=payload)
    assert rejected.status_code == 200
    assert rejected.json()["outcome"] == "rejected"
    assert "配额" in rejected.json()["reason"]
    assert client.get("/api/downlink/messages").json()["items"] == []

    decisions = client.get("/api/downlink/decisions", params={"decision": "reject"}).json()["items"]
    assert len(decisions) == 1
    assert decisions[0]["snapshot"]["message"]["message_key"] == "msg-0001"

    set_quota(client, "sat-x", 1000)
    admitted = client.post("/api/downlink/reservations", json=payload)
    assert admitted.json()["outcome"] == "reserved"

    replay = client.post("/api/downlink/reservations", json=payload)
    assert replay.json()["outcome"] == "replayed"
    assert replay.json()["message"]["id"] == admitted.json()["message"]["id"]
    assert replay.json()["reservation"]["id"] == admitted.json()["reservation"]["id"]

    assert len(client.get("/api/downlink/messages").json()["items"]) == 1
    assert len(client.get("/api/downlink/decisions", params={"decision": "reserve"}).json()["items"]) == 1
    slice_view = client.get("/api/downlink/slices").json()["items"][0]
    assert slice_view["used_mbps"] == 10

    changed = client.post("/api/downlink/reservations", json=reserve_payload("sat-x", "msg-0001", 45, required_mbps=10))
    assert changed.status_code == 409


def test_reserve_decision_keeps_bandwidth_snapshot(client):
    register_slice(client, "window-snapshot", -30, 30, 100)
    set_quota(client, "sat-y", 1000)
    response = client.post("/api/downlink/reservations", json=reserve_payload("sat-y", "msg-snap", 90, required_mbps=40))
    assert response.json()["outcome"] == "reserved"

    decisions = client.get("/api/downlink/decisions", params={"decision": "reserve"}).json()["items"]
    assert len(decisions) == 1
    snapshot = decisions[0]["snapshot"]
    assert snapshot["slice"]["total_mbps"] == 100
    assert snapshot["slice"]["used_mbps"] == 40
    assert snapshot["slice"]["available_mbps"] == 60
    assert snapshot["tenant"] == {"tenant": "sat-y", "quota_mb": 1000, "used_mb": 90, "remaining_mb": 910}
    assert snapshot["message"]["message_key"] == "msg-snap"


def test_quota_exhaustion_rejects_even_emergency(client):
    register_slice(client, "window-cap", -30, 30, 100)
    set_quota(client, "sat-z", 100)
    first = client.post("/api/downlink/reservations", json=reserve_payload("sat-z", "cap-1", 80, required_mbps=10))
    assert first.json()["outcome"] == "reserved"

    emergency = client.post("/api/downlink/reservations", json=reserve_payload("sat-z", "cap-2", 50, priority=99, required_mbps=10))
    assert emergency.json()["outcome"] == "rejected"
    assert "配额" in emergency.json()["reason"]

    decisions = client.get("/api/downlink/decisions", params={"decision": "reject", "tenant": "sat-z"}).json()["items"]
    assert len(decisions) == 1
    assert "配额" in decisions[0]["reason"]
    tenant_view = client.get("/api/downlink/tenants/sat-z").json()
    assert tenant_view["usage"] == [{"slice_id": first.json()["reservation"]["slice_id"], "code": "window-cap", "used_mb": 80}]


def test_bandwidth_shortage_defers_then_next_slice_migration(client):
    set_quota(client, "sat-d", 10000)
    register_slice(client, "window-full", -30, 30, 10)
    first = client.post("/api/downlink/reservations", json=reserve_payload("sat-d", "bulk-1", 10, required_mbps=10))
    assert first.json()["outcome"] == "reserved"

    second = client.post("/api/downlink/reservations", json=reserve_payload("sat-d", "bulk-2", 10, required_mbps=5))
    assert second.json()["outcome"] == "deferred"
    assert "带宽不足" in second.json()["reason"]

    created = register_slice(client, "window-next", 60, 120, 10)
    message_id = second.json()["message"]["id"]
    migrated = created["migration"]["migrated"]
    assert len(migrated) == 1
    assert migrated[0]["message_id"] == message_id
    assert migrated[0]["slice_id"] == created["slice"]["id"]

    detail = client.get(f"/api/downlink/messages/{message_id}").json()
    assert detail["message"]["status"] == "reserved"
    assert detail["reservations"][0]["slice_id"] == created["slice"]["id"]
    assert detail["reservations"][0]["mbps"] == 5
    kinds = [item["decision"] for item in detail["decisions"]]
    assert kinds == ["defer", "migrate"]


def test_emergency_preempts_normal_and_records_reason(client):
    set_quota(client, "sat-normal", 10000)
    set_quota(client, "sat-urgent", 10000)
    register_slice(client, "window-busy", -30, 30, 100)
    normal = client.post("/api/downlink/reservations", json=reserve_payload("sat-normal", "telemetry-1", 100, priority=20, required_mbps=80))
    assert normal.json()["outcome"] == "reserved"

    urgent = client.post("/api/downlink/reservations", json=reserve_payload("sat-urgent", "alarm-1", 100, priority=95, required_mbps=50))
    assert urgent.json()["outcome"] == "reserved"
    assert urgent.json()["preempted"] == [normal.json()["message"]["id"]]

    normal_view = client.get(f"/api/downlink/messages/{normal.json()['message']['id']}").json()
    assert normal_view["message"]["status"] == "preempted"
    assert normal_view["reservations"][0]["status"] == "preempted"

    preemptions = client.get("/api/downlink/decisions", params={"decision": "preempt"}).json()["items"]
    assert len(preemptions) == 1
    assert preemptions[0]["reason"] == "紧急遥测抢占普通数据"
    assert preemptions[0]["snapshot"]["slice"]["used_mbps"] == 0

    created = register_slice(client, "window-later", 60, 120, 100)
    assert [item["message_id"] for item in created["migration"]["migrated"]] == [normal.json()["message"]["id"]]

    # 管理员显式抢占一条仍在占用链路的预留
    extra = client.post("/api/downlink/reservations", json=reserve_payload("sat-normal", "telemetry-2", 10, priority=30, required_mbps=10))
    reservation_id = extra.json()["reservation"]["id"]
    manual = client.post(f"/api/downlink/reservations/{reservation_id}/preempt", json={"actor": "admin", "reason": "人工调度让路"})
    assert manual.status_code == 200
    assert manual.json()["message"]["status"] == "preempted"
    again = client.post(f"/api/downlink/reservations/{reservation_id}/preempt", json={"actor": "admin", "reason": "重复操作"})
    assert again.status_code == 409


def test_release_completes_message_and_frees_bandwidth(client):
    set_quota(client, "sat-r", 10000)
    register_slice(client, "window-release", -30, 30, 100)
    reserved = client.post("/api/downlink/reservations", json=reserve_payload("sat-r", "rel-1", 10, required_mbps=60))
    reservation_id = reserved.json()["reservation"]["id"]
    assert client.get("/api/downlink/slices").json()["items"][0]["available_mbps"] == 40

    released = client.post(f"/api/downlink/reservations/{reservation_id}/release", json={"actor": "ground-1"})
    assert released.status_code == 200
    assert released.json()["message"]["status"] == "completed"
    assert released.json()["reservation"]["status"] == "released"
    assert client.get("/api/downlink/slices").json()["items"][0]["available_mbps"] == 100

    repeat = client.post(f"/api/downlink/reservations/{reservation_id}/release", json={"actor": "ground-1"})
    assert repeat.status_code == 409
    message_id = released.json()["message"]["id"]
    deferred = client.post(f"/api/downlink/messages/{message_id}/defer", json={"actor": "admin", "reason": "已完成的消息"})
    assert deferred.status_code == 409


def test_defer_then_explicit_migration(client):
    set_quota(client, "sat-m", 10000)
    first = register_slice(client, "window-one", -30, 30, 100)
    second = register_slice(client, "window-two", 60, 120, 100)
    reserved = client.post("/api/downlink/reservations", json=reserve_payload("sat-m", "defer-1", 10, required_mbps=30))
    message_id = reserved.json()["message"]["id"]

    deferred = client.post(f"/api/downlink/messages/{message_id}/defer", json={"actor": "admin", "reason": "让出当前窗口"})
    assert deferred.status_code == 200
    assert deferred.json()["message"]["status"] == "deferred"
    assert deferred.json()["reservation"]["status"] == "deferred"
    slices = {item["code"]: item for item in client.get("/api/downlink/slices").json()["items"]}
    assert slices["window-one"]["available_mbps"] == 100

    # 当前窗口被其他消息占满后，迁移规则将其安置到下一个有时间片
    filler = client.post("/api/downlink/reservations", json=reserve_payload("sat-m", "defer-2", 10, required_mbps=100))
    assert filler.json()["outcome"] == "reserved"
    migrated = client.post("/api/downlink/migrations", json={"actor": "admin"})
    assert migrated.status_code == 200
    assert [item["message_id"] for item in migrated.json()["migrated"]] == [message_id]
    detail = client.get(f"/api/downlink/messages/{message_id}").json()
    assert detail["message"]["status"] == "reserved"
    assert detail["reservations"][-1]["slice_id"] == second["slice"]["id"]
    assert first["slice"]["id"] != second["slice"]["id"]

    repeated = client.post(f"/api/downlink/messages/{message_id}/defer", json={"actor": "admin", "reason": "重复延期"})
    assert repeated.status_code == 200  # 重新占用链路的消息允许再次延期
    missing = client.post("/api/downlink/messages/9999/defer", json={"actor": "admin", "reason": "不存在"})
    assert missing.status_code == 404


def test_migration_follows_priority_order(client):
    set_quota(client, "sat-p", 10000)
    register_slice(client, "window-tight", -30, 30, 10)
    client.post("/api/downlink/reservations", json=reserve_payload("sat-p", "seed", 10, required_mbps=10))
    low = client.post("/api/downlink/reservations", json=reserve_payload("sat-p", "low", 10, priority=10, required_mbps=5))
    high = client.post("/api/downlink/reservations", json=reserve_payload("sat-p", "high", 10, priority=80, required_mbps=5))
    assert low.json()["outcome"] == high.json()["outcome"] == "deferred"

    created = register_slice(client, "window-spare", 60, 120, 5)
    assert [item["message_id"] for item in created["migration"]["migrated"]] == [high.json()["message"]["id"]]
    assert created["migration"]["waiting"] == [{"message_id": low.json()["message"]["id"], "reason": "时间片剩余带宽不足"}]
    assert client.get(f"/api/downlink/messages/{low.json()['message']['id']}").json()["message"]["status"] == "deferred"


def test_required_mbps_validation(client):
    set_quota(client, "sat-v", 10**8)
    register_slice(client, "window-validate", -5, 5, 100)
    too_small = client.post("/api/downlink/reservations", json=reserve_payload("sat-v", "val-1", 300, required_mbps=2))
    assert too_small.status_code == 422
    too_large = client.post("/api/downlink/reservations", json=reserve_payload("sat-v", "val-2", 300, required_mbps=200))
    assert too_large.status_code == 422
    oversized = client.post("/api/downlink/reservations", json=reserve_payload("sat-v", "val-3", 100000))
    assert oversized.json()["outcome"] == "rejected"
    assert "传输能力" in oversized.json()["reason"]


def test_settle_requires_ended_slice_and_runs_once(client):
    current = register_slice(client, "window-open", -30, 30, 100)
    early = client.post("/api/downlink/settlements", json={"slice_id": current["slice"]["id"], "actor": "admin"})
    assert early.status_code == 409

    past = register_slice(client, "window-past", -120, -60, 100)
    settled = client.post("/api/downlink/settlements", json={"slice_id": past["slice"]["id"], "actor": "admin"})
    assert settled.status_code == 200
    assert settled.json()["items"] == []
    assert settled.json()["slice"]["status"] == "settled"

    repeat = client.post("/api/downlink/settlements", json={"slice_id": past["slice"]["id"], "actor": "admin"})
    assert repeat.status_code == 409
    assert client.get("/api/downlink/bills").json()["items"] == []


def test_settle_bills_completed_transfers_once_and_migrates_unfinished(client):
    clock = FrozenClock(datetime(2026, 9, 26, 2, 0, tzinfo=UTC))
    service = DownlinkService(get_connection(), clock)
    service.set_quota("sat-a", {"quota_mb": 1000, "price_per_mb": 2.0}, "admin")
    service.set_quota("sat-b", {"quota_mb": 1000, "price_per_mb": 1.0}, "admin")
    window = service.register_slice(
        {"code": "settle-w1", "starts_at": datetime(2026, 9, 26, 2, 0, tzinfo=UTC), "ends_at": datetime(2026, 9, 26, 2, 10, tzinfo=UTC), "total_mbps": 100},
        "admin",
    )["slice"]

    done = service.reserve({"tenant": "sat-a", "message_key": "done-1", "size_mb": 30, "priority": 50})
    unfinished = service.reserve({"tenant": "sat-b", "message_key": "slow-1", "size_mb": 40, "priority": 60})
    assert done["outcome"] == unfinished["outcome"] == "reserved"
    service.release(done["reservation"]["id"], "ground-1")

    with pytest.raises(ConflictError):
        service.settle(window["id"], "admin")

    clock.advance(minutes=11)
    result = service.settle(window["id"], "admin")
    assert result["totals"] == {"sat-a": 60.0}
    assert [item["message_id"] for item in result["items"]] == [done["message"]["id"]]
    assert result["items"][0]["amount"] == 60.0
    assert result["migrated_out"] == [unfinished["message"]["id"]]
    assert service.get_message(unfinished["message"]["id"])["message"]["status"] == "deferred"

    with pytest.raises(ConflictError):
        service.settle(window["id"], "admin")

    bills = client.get("/api/downlink/bills").json()["items"]
    assert len(bills) == 1
    assert bills[0]["tenant"] == "sat-a"
    summary = client.get("/api/downlink/bills/summary").json()["items"]
    assert summary == [{"tenant": "sat-a", "items": 1, "total_mb": 30, "total_amount": 60.0}]

    # 结算后未完成的消息按迁移规则进入下一个时间片，不重复计费
    follow = service.register_slice(
        {"code": "settle-w2", "starts_at": datetime(2026, 9, 26, 2, 11, tzinfo=UTC), "ends_at": datetime(2026, 9, 26, 2, 21, tzinfo=UTC), "total_mbps": 100},
        "admin",
    )
    assert [item["message_id"] for item in follow["migration"]["migrated"]] == [unfinished["message"]["id"]]
    detail = service.get_message(unfinished["message"]["id"])
    assert detail["message"]["status"] == "reserved"
    assert len(detail["reservations"]) == 2
    assert [item["decision"] for item in detail["decisions"]] == ["reserve", "migrate", "migrate"]
    assert client.get("/api/downlink/bills").json()["items"] == bills


def test_summary_and_message_filters(client):
    set_quota(client, "sat-s", 10000)
    register_slice(client, "window-summary", -30, 30, 100)
    client.post("/api/downlink/reservations", json=reserve_payload("sat-s", "sum-1", 10, required_mbps=10))
    client.post("/api/downlink/reservations", json=reserve_payload("sat-s", "sum-2", 10, required_mbps=95))

    summary = client.get("/api/downlink/summary").json()
    assert summary["messages"] == {"reserved": 1, "deferred": 1}
    assert summary["slices"]["scheduled"] == 1
    assert summary["bills"] == {"count": 0, "total_amount": 0}
    assert summary["decisions"] == 2

    deferred = client.get("/api/downlink/messages", params={"status": "deferred"}).json()["items"]
    assert [item["message_key"] for item in deferred] == ["sum-2"]
    by_tenant = client.get("/api/downlink/messages", params={"tenant": "sat-s"}).json()["items"]
    assert len(by_tenant) == 2
