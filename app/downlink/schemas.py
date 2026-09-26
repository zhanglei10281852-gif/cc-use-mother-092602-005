from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class SliceCreate(BaseModel):
    """登记链路时间片：起止时间使用 ISO 8601，未带时区按 UTC 处理。"""

    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    starts_at: datetime
    ends_at: datetime
    total_mbps: int = Field(gt=0, le=1_000_000)

    @model_validator(mode="after")
    def validate_window(self) -> "SliceCreate":
        if self.ends_at <= self.starts_at:
            raise ValueError("时间片结束时刻必须晚于开始时刻")
        return self


class QuotaSet(BaseModel):
    """租户在每个时间片内可结算的数据量上限与计费单价。"""

    quota_mb: int = Field(ge=0, le=1_000_000_000)
    price_per_mb: float = Field(default=1.0, ge=0, le=1_000_000)


class ReserveRequest(BaseModel):
    """为一条卫星消息预留链路带宽；同一租户下 message_key 唯一，重试幂等。"""

    tenant: str = Field(min_length=1, max_length=80)
    message_key: str = Field(min_length=1, max_length=160)
    size_mb: int = Field(gt=0, le=10_000_000)
    priority: int = Field(default=50, ge=0, le=100)
    slice_id: int | None = Field(default=None, gt=0)
    required_mbps: int | None = Field(default=None, gt=0, le=1_000_000)


class ReleaseRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)


class ActorReason(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)


class MigrateRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)


class SettleRequest(BaseModel):
    slice_id: int = Field(gt=0)
    actor: str = Field(min_length=1, max_length=120)
