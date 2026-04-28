"""Schemas for /v1/webhook-endpoints CRUD."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class WebhookEndpointIn(BaseModel):
    url: HttpUrl
    description: str = Field(default="", max_length=500)
    events: list[str] = Field(default_factory=list)
    scope: Literal["full"] = "full"  # 'selected' is plan §4.5 future work


class WebhookEndpointPatchIn(BaseModel):
    url: HttpUrl | None = None
    description: str | None = Field(default=None, max_length=500)
    events: list[str] | None = None
    enabled: bool | None = None


class WebhookEndpointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    url: str
    description: str
    events: list[str]
    enabled: bool
    scope: str
    created: datetime
    updated: datetime


class WebhookEndpointCreatedOut(WebhookEndpointOut):
    """Returned only at creation. Includes the raw signing secret — shown once."""
    signing_secret: str


class WebhookEndpointRotatedOut(BaseModel):
    id: uuid.UUID
    signing_secret: str


class WebhookDeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    endpoint_id: uuid.UUID
    event_id: str
    event_name: str
    idempotency_key: str
    attempts: int
    last_attempt_at: datetime | None
    last_status: int | None
    last_error: str | None
    delivered_at: datetime | None
    next_retry_at: datetime | None
    created_at: datetime


class WebhookDeliveryDetailOut(WebhookDeliveryOut):
    payload: dict
