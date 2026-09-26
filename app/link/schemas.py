from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class TimesliceCreate(BaseModel):
    code: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    starts_at: datetime
    ends_at: datetime
    total_kbps: int = Field(gt=0, le=10_000_000)
    price_per_kbps_s: float = Field(default=0.0, ge=0, le=1000)


class QuotaSet(BaseModel):
    tenant: str = Field(min_length=1, max_length=120)
    max_kbps_per_slice: int = Field(ge=0, le=10_000_000)
    max_active_reservations: int = Field(default=100, ge=0, le=100_000)


class PriorityClassCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    rank: int = Field(ge=0, le=1000)
    may_preempt: bool = False
    description: str = Field(default="", max_length=500)


class ReservationCreate(BaseModel):
    tenant: str = Field(min_length=1, max_length=120)
    message_key: str = Field(min_length=4, max_length=160)
    priority_class: str = Field(min_length=2, max_length=64)
    kbps: int = Field(gt=0, le=10_000_000)
    timeslice_id: int = Field(gt=0)


class ReleaseRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)


class PreemptRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)


class DeferRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)
    target_timeslice_id: int = Field(gt=0)


class RolloverRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    target_timeslice_id: int | None = Field(default=None, gt=0)
    reason: str = Field(default="时间片结束，迁移未完成传输", min_length=2, max_length=1000)


class SettleRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
