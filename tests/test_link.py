from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.clock import FrozenClock
from app.database import get_connection
from app.link.service import LinkCoordinationService

BASE = datetime(2026, 9, 26, 8, 0, tzinfo=UTC)


def iso(value: datetime) -> str:
    return value.isoformat()


def register_classes(client) -> None:
    urgent = client.post(
        "/api/link/priority-classes?actor=admin",
        json={"code": "emergency", "rank": 100, "may_preempt": True, "description": "紧急遥测"},
    )
    assert urgent.status_code == 201, urgent.text
    bulk = client.post(
        "/api/link/priority-classes?actor=admin",
        json={"code": "bulk", "rank": 10, "may_preempt": False, "description": "普通数据"},
    )
    assert bulk.status_code == 201, bulk.text


def create_slice(client, code: str, *, total_kbps: int = 1000, price: float = 0.5, start: datetime = BASE, hours: int = 1) -> dict:
    response = client.post(
        "/api/link/timeslices?actor=admin",
        json={
            "code": code,
            "starts_at": iso(start),
            "ends_at": iso(start + timedelta(hours=hours)),
            "total_kbps": total_kbps,
            "price_per_kbps_s": price,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def reserve(client, tenant: str, key: str, klass: str, kbps: int, slice_id: int) -> dict:
    response = client.post(
        "/api/link/reservations",
        json={"tenant": tenant, "message_key": key, "priority_class": klass, "kbps": kbps, "timeslice_id": slice_id},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_timeslice_and_priority_registration(client):
    register_classes(client)
    duplicate = client.post("/api/link/priority-classes?actor=admin", json={"code": "bulk", "rank": 5, "may_preempt": False})
    assert duplicate.status_code == 409
    slice_a = create_slice(client, "pass-a")
    assert slice_a["status"] == "open"
    again = client.post(
        "/api/link/timeslices?actor=admin",
        json={"code": "pass-a", "starts_at": iso(BASE), "ends_at": iso(BASE + timedelta(hours=1)), "total_kbps": 100},
    )
    assert again.status_code == 409
    invalid = client.post(
        "/api/link/timeslices?actor=admin",
        json={"code": "pass-bad", "starts_at": iso(BASE), "ends_at": iso(BASE), "total_kbps": 100},
    )
    assert invalid.status_code == 422
    listing = client.get("/api/link/timeslices").json()["items"]
    assert listing[0]["available_kbps"] == 1000 and listing[0]["reserved_kbps"] == 0
    classes = client.get("/api/link/priority-classes").json()["items"]
    assert [item["code"] for item in classes] == ["emergency", "bulk"]


def test_reserve_retry_does_not_double_deduct(client):
    register_classes(client)
    slice_a = create_slice(client, "pass-a")
    first = reserve(client, "sat-1", "msg-0001", "bulk", 400, slice_a["id"])
    assert first["status"] == "reserved"
    second = reserve(client, "sat-1", "msg-0001", "bulk", 400, slice_a["id"])
    assert second["id"] == first["id"]
    detail = client.get(f"/api/link/timeslices/{slice_a['id']}").json()
    assert detail["snapshot"]["reserved_kbps"] == 400
    assert detail["snapshot"]["by_tenant"] == {"sat-1": 400}
    conflict = client.post(
        "/api/link/reservations",
        json={"tenant": "sat-1", "message_key": "msg-0001", "priority_class": "bulk", "kbps": 500, "timeslice_id": slice_a["id"]},
    )
    assert conflict.status_code == 409
    decisions = client.get(f"/api/link/decisions?timeslice_id={slice_a['id']}").json()["items"]
    assert [item["decision"] for item in decisions] == ["reserve"]
    assert decisions[0]["snapshot"]["reserved_kbps"] == 400
    assert decisions[0]["snapshot"]["available_kbps"] == 600


def test_quota_rejection_is_recorded_and_replayable(client):
    register_classes(client)
    slice_a = create_slice(client, "pass-a")
    quota = client.put("/api/link/quotas?actor=admin", json={"tenant": "sat-limited", "max_kbps_per_slice": 300, "max_active_reservations": 1})
    assert quota.status_code == 200
    rejected = reserve(client, "sat-limited", "msg-over", "bulk", 400, slice_a["id"])
    assert rejected["status"] == "rejected"
    assert "配额" in rejected["status_reason"]
    replay = reserve(client, "sat-limited", "msg-over", "bulk", 400, slice_a["id"])
    assert replay["id"] == rejected["id"] and replay["status"] == "rejected"
    ok = reserve(client, "sat-limited", "msg-ok", "bulk", 200, slice_a["id"])
    assert ok["status"] == "reserved"
    blocked = reserve(client, "sat-limited", "msg-count", "bulk", 100, slice_a["id"])
    assert blocked["status"] == "rejected" and "预留数" in blocked["status_reason"]
    listing = client.get("/api/link/reservations?status=rejected").json()["items"]
    assert {item["message_key"] for item in listing} == {"msg-over", "msg-count"}
    rejects = client.get("/api/link/decisions?decision=reject").json()["items"]
    assert len(rejects) == 2
    assert all(item["snapshot"]["timeslice_id"] == slice_a["id"] for item in rejects)
    assert client.get(f"/api/link/timeslices/{slice_a['id']}").json()["snapshot"]["reserved_kbps"] == 200


def test_emergency_preempts_bulk_and_quota_still_bounds_emergency(client):
    register_classes(client)
    slice_a = create_slice(client, "pass-a")
    bulk_one = reserve(client, "sat-n", "bulk-1", "bulk", 400, slice_a["id"])
    bulk_two = reserve(client, "sat-n", "bulk-2", "bulk", 400, slice_a["id"])
    urgent = reserve(client, "sat-u", "urgent-1", "emergency", 500, slice_a["id"])
    assert urgent["status"] == "reserved"
    after_two = client.get(f"/api/link/reservations/{bulk_two['id']}").json()
    assert after_two["status"] == "preempted" and "urgent-1" in after_two["status_reason"]
    after_one = client.get(f"/api/link/reservations/{bulk_one['id']}").json()
    assert after_one["status"] == "reserved"
    snapshot = client.get(f"/api/link/timeslices/{slice_a['id']}").json()["snapshot"]
    assert snapshot["reserved_kbps"] == 900
    assert snapshot["by_priority_class"] == {"bulk": 400, "emergency": 500}
    preempts = client.get("/api/link/decisions?decision=preempt").json()["items"]
    assert len(preempts) == 1 and preempts[0]["reservation_id"] == bulk_two["id"]
    assert preempts[0]["snapshot"]["reserved_kbps"] == 400
    client.put("/api/link/quotas?actor=admin", json={"tenant": "sat-u", "max_kbps_per_slice": 600, "max_active_reservations": 10})
    blocked = reserve(client, "sat-u", "urgent-2", "emergency", 200, slice_a["id"])
    assert blocked["status"] == "rejected" and "配额" in blocked["status_reason"]
    preempted = client.get("/api/link/reservations?status=preempted").json()["items"]
    assert [item["message_key"] for item in preempted] == ["bulk-2"]


def test_release_is_idempotent_and_restores_bandwidth(client):
    register_classes(client)
    slice_a = create_slice(client, "pass-a")
    reservation = reserve(client, "sat-1", "msg-rel", "bulk", 300, slice_a["id"])
    released = client.post(f"/api/link/reservations/{reservation['id']}/release", json={"actor": "ops", "reason": "传输完成"})
    assert released.status_code == 200 and released.json()["status"] == "released"
    replay = client.post(f"/api/link/reservations/{reservation['id']}/release", json={"actor": "ops", "reason": "传输完成"})
    assert replay.status_code == 200 and replay.json()["status"] == "released"
    snapshot = client.get(f"/api/link/timeslices/{slice_a['id']}").json()["snapshot"]
    assert snapshot["reserved_kbps"] == 0
    releases = client.get(f"/api/link/decisions?timeslice_id={slice_a['id']}&decision=release").json()["items"]
    assert len(releases) == 1
    rejected = reserve(client, "sat-1", "msg-rej", "bulk", 2000, slice_a["id"])
    assert rejected["status"] == "rejected"
    cannot = client.post(f"/api/link/reservations/{rejected['id']}/release", json={"actor": "ops", "reason": "无意义"})
    assert cannot.status_code == 409


def test_manual_preempt_records_reason_for_admin(client):
    register_classes(client)
    slice_a = create_slice(client, "pass-a")
    reservation = reserve(client, "sat-1", "msg-pre", "bulk", 300, slice_a["id"])
    preempted = client.post(f"/api/link/reservations/{reservation['id']}/preempt", json={"actor": "admin", "reason": "紧急遥测需要链路"})
    assert preempted.status_code == 200
    body = preempted.json()
    assert body["status"] == "preempted" and body["status_reason"] == "紧急遥测需要链路"
    again = client.post(f"/api/link/reservations/{reservation['id']}/preempt", json={"actor": "admin", "reason": "重复操作"})
    assert again.status_code == 409
    detail = client.get(f"/api/link/reservations/{reservation['id']}").json()
    assert [item["decision"] for item in detail["decisions"]] == ["reserve", "preempt"]
    assert detail["decisions"][1]["reason"] == "紧急遥测需要链路"
    assert detail["ledger"][0]["end_reason"] == "preempt"
    assert client.get(f"/api/link/timeslices/{slice_a['id']}").json()["snapshot"]["reserved_kbps"] == 0


def test_defer_moves_reservation_and_handles_full_target(client):
    register_classes(client)
    slice_a = create_slice(client, "pass-a", total_kbps=500)
    slice_b = create_slice(client, "pass-b", total_kbps=1000, start=BASE + timedelta(hours=1))
    slice_c = create_slice(client, "pass-c", total_kbps=100, start=BASE + timedelta(hours=2))
    reservation = reserve(client, "sat-1", "msg-def", "bulk", 400, slice_a["id"])
    moved = client.post(
        f"/api/link/reservations/{reservation['id']}/defer",
        json={"actor": "ops", "reason": "避开检修窗口", "target_timeslice_id": slice_b["id"]},
    )
    assert moved.status_code == 200
    assert moved.json()["status"] == "reserved" and moved.json()["timeslice_id"] == slice_b["id"]
    assert client.get(f"/api/link/timeslices/{slice_a['id']}").json()["snapshot"]["reserved_kbps"] == 0
    same = client.post(
        f"/api/link/reservations/{reservation['id']}/defer",
        json={"actor": "ops", "reason": "原地延期", "target_timeslice_id": slice_b["id"]},
    )
    assert same.status_code == 409
    failed = client.post(
        f"/api/link/reservations/{reservation['id']}/defer",
        json={"actor": "ops", "reason": "再次调整", "target_timeslice_id": slice_c["id"]},
    )
    assert failed.status_code == 200
    assert failed.json()["status"] == "deferred" and "带宽不足" in failed.json()["status_reason"]
    recovered = client.post(
        f"/api/link/reservations/{reservation['id']}/defer",
        json={"actor": "ops", "reason": "重新安置", "target_timeslice_id": slice_b["id"]},
    )
    assert recovered.json()["status"] == "reserved"
    detail = client.get(f"/api/link/reservations/{reservation['id']}").json()
    assert [entry["end_reason"] for entry in detail["ledger"]] == ["defer", "defer", ""]
    released = client.post(f"/api/link/reservations/{reservation['id']}/release", json={"actor": "ops", "reason": "任务取消"})
    assert released.status_code == 200
    terminal = client.post(
        f"/api/link/reservations/{reservation['id']}/defer",
        json={"actor": "ops", "reason": "已释放", "target_timeslice_id": slice_b["id"]},
    )
    assert terminal.status_code == 409


def test_rollover_migrates_by_priority_and_marks_rest(client):
    register_classes(client)
    slice_a = create_slice(client, "pass-a", total_kbps=1000)
    slice_b = create_slice(client, "pass-b", total_kbps=400, start=BASE + timedelta(hours=1))
    urgent = reserve(client, "sat-u", "urgent-m", "emergency", 300, slice_a["id"])
    bulk_one = reserve(client, "sat-n", "bulk-m1", "bulk", 200, slice_a["id"])
    bulk_two = reserve(client, "sat-n", "bulk-m2", "bulk", 200, slice_a["id"])
    result = client.post(f"/api/link/timeslices/{slice_a['id']}/rollover", json={"actor": "ops", "target_timeslice_id": slice_b["id"]})
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["timeslice"]["status"] == "closed"
    assert body["migrated"] == [urgent["id"]]
    assert set(body["deferred"]) == {bulk_one["id"], bulk_two["id"]}
    assert body["expired"] == []
    migrated = client.get(f"/api/link/reservations/{urgent['id']}").json()
    assert migrated["status"] == "reserved" and migrated["timeslice_id"] == slice_b["id"]
    waiting = client.get(f"/api/link/reservations/{bulk_one['id']}").json()
    assert waiting["status"] == "deferred" and waiting["timeslice_id"] is None
    assert "带宽不足" in waiting["status_reason"]
    late = reserve(client, "sat-n", "bulk-late", "bulk", 100, slice_a["id"])
    assert late["status"] == "rejected" and "关闭" in late["status_reason"]
    again = client.post(f"/api/link/timeslices/{slice_a['id']}/rollover", json={"actor": "ops"})
    assert again.status_code == 409
    snapshot_b = client.get(f"/api/link/timeslices/{slice_b['id']}").json()["snapshot"]
    assert snapshot_b["reserved_kbps"] == 300


def test_rollover_auto_target_and_expiry(client):
    register_classes(client)
    slice_a = create_slice(client, "pass-a", total_kbps=300)
    slice_b = create_slice(client, "pass-b", total_kbps=300, start=BASE + timedelta(hours=1))
    keep = reserve(client, "sat-1", "msg-keep", "bulk", 200, slice_a["id"])
    auto = client.post(f"/api/link/timeslices/{slice_a['id']}/rollover", json={"actor": "ops"}).json()
    assert auto["target_timeslice_id"] == slice_b["id"] and auto["migrated"] == [keep["id"]]
    rollover_b = client.post(f"/api/link/timeslices/{slice_b['id']}/rollover", json={"actor": "ops"}).json()
    assert rollover_b["target_timeslice_id"] is None and rollover_b["expired"] == [keep["id"]]
    final = client.get(f"/api/link/reservations/{keep['id']}").json()
    assert final["status"] == "expired"
    assert [item["decision"] for item in final["decisions"]] == ["reserve", "migrate", "migrate"]


def test_settle_flow_and_bills_over_http(client):
    register_classes(client)
    slice_a = create_slice(client, "pass-a", total_kbps=1000, price=0.25)
    early = client.post(f"/api/link/timeslices/{slice_a['id']}/settle", json={"actor": "ops"})
    assert early.status_code == 409
    one = reserve(client, "sat-1", "msg-s1", "bulk", 100, slice_a["id"])
    two = reserve(client, "sat-2", "msg-s2", "bulk", 200, slice_a["id"])
    client.post(f"/api/link/reservations/{two['id']}/release", json={"actor": "ops", "reason": "传完"})
    rollover = client.post(f"/api/link/timeslices/{slice_a['id']}/rollover", json={"actor": "ops"})
    assert rollover.json()["expired"] == [one["id"]]
    settled = client.post(f"/api/link/timeslices/{slice_a['id']}/settle", json={"actor": "ops"})
    assert settled.status_code == 200, settled.text
    bills = settled.json()["bills"]
    assert {bill["tenant"] for bill in bills} == {"sat-1", "sat-2"}
    assert settled.json()["timeslice"]["status"] == "settled"
    again = client.post(f"/api/link/timeslices/{slice_a['id']}/settle", json={"actor": "ops"})
    assert again.status_code == 409
    sat1 = client.get("/api/link/bills?tenant=sat-1").json()["items"]
    assert len(sat1) == 1 and sat1[0]["timeslice_id"] == slice_a["id"]
    assert sat1[0]["details"][0]["message_key"] == "msg-s1"
    settles = client.get(f"/api/link/decisions?decision=settle&timeslice_id={slice_a['id']}").json()["items"]
    assert len(settles) == 1 and settles[0]["snapshot"]["status"] == "settled"


def test_billing_and_migration_with_frozen_clock(client):
    clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=UTC))
    service = LinkCoordinationService(get_connection(), clock)
    service.create_priority_class({"code": "emergency", "rank": 100, "may_preempt": True, "description": ""}, "admin")
    service.create_priority_class({"code": "bulk", "rank": 10, "may_preempt": False, "description": ""}, "admin")
    slice_a = service.create_timeslice(
        {"code": "slice-a", "starts_at": clock.now(), "ends_at": clock.now() + timedelta(hours=1), "total_kbps": 1000, "price_per_kbps_s": 0.5},
        "admin",
    )
    slice_b = service.create_timeslice(
        {"code": "slice-b", "starts_at": clock.now() + timedelta(hours=1), "ends_at": clock.now() + timedelta(hours=2), "total_kbps": 1000, "price_per_kbps_s": 1.0},
        "admin",
    )
    stay = service.reserve({"tenant": "sat-1", "message_key": "stay-1", "priority_class": "bulk", "kbps": 100, "timeslice_id": slice_a["id"]})
    leave = service.reserve({"tenant": "sat-1", "message_key": "leave-1", "priority_class": "bulk", "kbps": 50, "timeslice_id": slice_a["id"]})
    clock.advance(seconds=600)
    service.release(leave["id"], "ops", "传输完成")
    clock.advance(seconds=600)
    result = service.rollover(slice_a["id"], "ops", slice_b["id"])
    assert result["migrated"] == [stay["id"]]
    settled_a = service.settle(slice_a["id"], "ops")
    bills_a = {bill["tenant"]: bill for bill in settled_a["bills"]}
    assert bills_a["sat-1"]["kbps_seconds"] == 100 * 1200 + 50 * 600
    assert bills_a["sat-1"]["amount"] == (100 * 1200 + 50 * 600) * 0.5
    details = {item["message_key"]: item for item in bills_a["sat-1"]["details"]}
    assert details["stay-1"]["seconds"] == 1200 and details["leave-1"]["seconds"] == 600
    clock.advance(seconds=300)
    service.release(stay["id"], "ops", "传输完成")
    service.rollover(slice_b["id"], "ops")
    settled_b = service.settle(slice_b["id"], "ops")
    bills_b = {bill["tenant"]: bill for bill in settled_b["bills"]}
    assert bills_b["sat-1"]["kbps_seconds"] == 100 * 300
    assert bills_b["sat-1"]["amount"] == 100 * 300 * 1.0
    decisions = service.list_decisions(timeslice_id=slice_a["id"])
    assert [item["decision"] for item in decisions] == ["reserve", "reserve", "release", "settle"]
    decisions_b = service.list_decisions(timeslice_id=slice_b["id"])
    assert [item["decision"] for item in decisions_b] == ["migrate", "release", "settle"]
    assert decisions_b[0]["snapshot"]["reserved_kbps"] == 100
