from __future__ import annotations

from fastapi import APIRouter, Query

from app.link.schemas import (
    DeferRequest,
    PreemptRequest,
    PriorityClassCreate,
    QuotaSet,
    ReleaseRequest,
    ReservationCreate,
    RolloverRequest,
    SettleRequest,
    TimesliceCreate,
)
from app.link.service import LinkCoordinationService

router = APIRouter(prefix="/api/link", tags=["卫星链路带宽协调"])


def service() -> LinkCoordinationService:
    return LinkCoordinationService()


@router.post("/timeslices", status_code=201)
def create_timeslice(payload: TimesliceCreate, actor: str = Query(..., min_length=1)):
    return service().create_timeslice(payload.model_dump(), actor)


@router.get("/timeslices")
def list_timeslices(status: str | None = None):
    return {"items": service().list_timeslices(status=status)}


@router.get("/timeslices/{timeslice_id}")
def get_timeslice(timeslice_id: int):
    return service().get_timeslice(timeslice_id)


@router.post("/timeslices/{timeslice_id}/rollover")
def rollover_timeslice(timeslice_id: int, payload: RolloverRequest):
    return service().rollover(timeslice_id, payload.actor, payload.target_timeslice_id, payload.reason)


@router.post("/timeslices/{timeslice_id}/settle")
def settle_timeslice(timeslice_id: int, payload: SettleRequest):
    return service().settle(timeslice_id, payload.actor)


@router.put("/quotas")
def set_quota(payload: QuotaSet, actor: str = Query(..., min_length=1)):
    return service().set_quota(payload.model_dump(), actor)


@router.get("/quotas")
def list_quotas():
    return {"items": service().list_quotas()}


@router.post("/priority-classes", status_code=201)
def create_priority_class(payload: PriorityClassCreate, actor: str = Query(..., min_length=1)):
    return service().create_priority_class(payload.model_dump(), actor)


@router.get("/priority-classes")
def list_priority_classes():
    return {"items": service().list_priority_classes()}


@router.post("/reservations", status_code=201)
def create_reservation(payload: ReservationCreate):
    return service().reserve(payload.model_dump())


@router.get("/reservations")
def list_reservations(
    tenant: str | None = None,
    status: str | None = None,
    timeslice_id: int | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    return {"items": service().list_reservations(tenant=tenant, status=status, timeslice_id=timeslice_id, limit=limit)}


@router.get("/reservations/{reservation_id}")
def get_reservation(reservation_id: int):
    return service().get_reservation(reservation_id)


@router.post("/reservations/{reservation_id}/release")
def release_reservation(reservation_id: int, payload: ReleaseRequest):
    return service().release(reservation_id, payload.actor, payload.reason)


@router.post("/reservations/{reservation_id}/preempt")
def preempt_reservation(reservation_id: int, payload: PreemptRequest):
    return service().preempt(reservation_id, payload.actor, payload.reason)


@router.post("/reservations/{reservation_id}/defer")
def defer_reservation(reservation_id: int, payload: DeferRequest):
    return service().defer(reservation_id, payload.actor, payload.reason, payload.target_timeslice_id)


@router.get("/decisions")
def list_decisions(
    timeslice_id: int | None = None,
    decision: str | None = None,
    tenant: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
):
    return {"items": service().list_decisions(timeslice_id=timeslice_id, decision=decision, tenant=tenant, limit=limit)}


@router.get("/bills")
def list_bills(tenant: str | None = None, timeslice_id: int | None = None):
    return {"items": service().list_bills(tenant=tenant, timeslice_id=timeslice_id)}
