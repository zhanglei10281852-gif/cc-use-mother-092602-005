from __future__ import annotations

from fastapi import APIRouter, Query

from app.downlink.schemas import ActorReason, MigrateRequest, QuotaSet, ReleaseRequest, ReserveRequest, SettleRequest, SliceCreate
from app.downlink.service import DownlinkService

router = APIRouter(prefix="/api/downlink", tags=["卫星地面链路协调"])


def service() -> DownlinkService:
    return DownlinkService()


@router.get("/slices")
def list_slices():
    return {"items": service().list_slices()}


@router.post("/slices", status_code=201)
def register_slice(payload: SliceCreate, actor: str = Query(..., min_length=1)):
    return service().register_slice(payload.model_dump(), actor)


@router.put("/tenants/{tenant}/quota")
def set_quota(tenant: str, payload: QuotaSet, actor: str = Query(..., min_length=1)):
    return service().set_quota(tenant, payload.model_dump(), actor)


@router.get("/tenants/{tenant}")
def get_tenant(tenant: str):
    return service().get_tenant(tenant)


@router.post("/reservations")
def reserve(payload: ReserveRequest):
    return service().reserve(payload.model_dump())


@router.post("/reservations/{reservation_id}/release")
def release(reservation_id: int, payload: ReleaseRequest):
    return service().release(reservation_id, payload.actor)


@router.post("/reservations/{reservation_id}/preempt")
def preempt(reservation_id: int, payload: ActorReason):
    return service().preempt(reservation_id, payload.actor, payload.reason)


@router.post("/messages/{message_id}/defer")
def defer(message_id: int, payload: ActorReason):
    return service().defer(message_id, payload.actor, payload.reason)


@router.post("/migrations")
def migrate(payload: MigrateRequest):
    return service().migrate(payload.actor)


@router.post("/settlements")
def settle(payload: SettleRequest):
    return service().settle(payload.slice_id, payload.actor)


@router.get("/messages")
def list_messages(status: str | None = None, tenant: str | None = None, limit: int = Query(default=100, ge=1, le=500)):
    return {"items": service().list_messages(status=status, tenant=tenant, limit=limit)}


@router.get("/messages/{message_id}")
def get_message(message_id: int):
    return service().get_message(message_id)


@router.get("/decisions")
def list_decisions(
    decision: str | None = None,
    tenant: str | None = None,
    message_id: int | None = None,
    slice_id: int | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    return {"items": service().list_decisions(decision=decision, tenant=tenant, message_id=message_id, slice_id=slice_id, limit=limit)}


@router.get("/bills")
def list_bills(tenant: str | None = None, slice_id: int | None = None, limit: int = Query(default=100, ge=1, le=500)):
    return {"items": service().list_bills(tenant=tenant, slice_id=slice_id, limit=limit)}


@router.get("/bills/summary")
def bills_summary():
    return {"items": service().bills_summary()}


@router.get("/summary")
def summary():
    return service().summary()
