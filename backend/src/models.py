"""Pydantic request schemas for NexusAI Retail Intelligence integrations.

These schemas define the vendor-facing POS payload contract. They do not enable
an endpoint by themselves; route handlers must explicitly use these types.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class APIRequestModel(BaseModel):
    """Shared strict request defaults for external integration payloads."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class POSExceptionEventIn(APIRequestModel):
    """One POS transaction/exception event sent by an authorized POS adapter."""

    site_id: str = Field(min_length=1, max_length=100)
    source_event_id: str = Field(
        min_length=1,
        max_length=200,
        description="Stable idempotency key unique within the documented site/provider scope.",
    )
    till_id: str = Field(min_length=1, max_length=128)
    exception_type: Literal[
        "refund",
        "return",
        "return_issued",
        "void",
        "transaction_void",
        "line_item_cancelled",
        "no_sale",
        "drawer_open",
        "manager_override",
        "manual_discount",
    ]
    transaction_id: str | None = Field(default=None, max_length=160)
    cashier_id: str | None = Field(default=None, max_length=160)
    cashier_name: str | None = Field(default=None, max_length=200)
    transaction_value: Decimal = Field(
        ge=Decimal("0"),
        le=Decimal("100000000"),
        max_digits=14,
        decimal_places=2,
        description="Amount in the stated currency; vendor must document whether this is gross, reversed, or adjusted value.",
    )
    currency: str = Field(default="ZAR", min_length=3, max_length=3)
    occurred_at: datetime = Field(
        description="ISO 8601 timestamp including an explicit UTC offset, e.g. 2026-10-19T14:30:00+02:00."
    )
    active_transaction: bool | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at", mode="before")
    @classmethod
    def require_timezone_offset(cls, value: Any) -> Any:
        if isinstance(value, str):
            candidate = value.strip()
            # datetime.fromisoformat accepts Z on supported Python versions.
            try:
                parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("occurred_at must be a valid ISO 8601 timestamp.") from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("occurred_at must include an explicit UTC offset.")
            return parsed
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("occurred_at must include an explicit UTC offset.")
            return value
        return value

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        normalized = value.upper()
        if not normalized.isalpha():
            raise ValueError("currency must be a three-letter ISO currency code.")
        return normalized


class POSExceptionBatchIn(APIRequestModel):
    """A bounded batch of POS exception events for secure batch-ingestion adapters."""

    events: list[POSExceptionEventIn] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def require_unique_event_keys_within_batch(self) -> "POSExceptionBatchIn":
        keys = [(event.site_id, event.source_event_id) for event in self.events]
        if len(keys) != len(set(keys)):
            raise ValueError("events must not contain duplicate (site_id, source_event_id) pairs.")
        return self


class VideoTimelineReferenceIn(APIRequestModel):
    """Reference to an existing video timeline/clip; does not upload video."""

    site_id: str = Field(min_length=1, max_length=100)
    camera_id: str = Field(min_length=1, max_length=128)
    clip_start: datetime
    clip_end: datetime
    video_clip_url: str = Field(min_length=10, max_length=2048)
    source_ref_id: str | None = Field(default=None, max_length=200)

    @field_validator("clip_start", "clip_end", mode="before")
    @classmethod
    def require_timezone_on_clip_times(cls, value: Any) -> Any:
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("Clip timestamps must be valid ISO 8601 timestamps.") from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("Clip timestamps must include an explicit UTC offset.")
            return parsed
        if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Clip timestamps must include an explicit UTC offset.")
        return value

    @model_validator(mode="after")
    def validate_clip_interval(self) -> "VideoTimelineReferenceIn":
        if self.clip_end <= self.clip_start:
            raise ValueError("clip_end must be after clip_start.")
        return self

    @field_validator("video_clip_url")
    @classmethod
    def require_https_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("video_clip_url must be an HTTPS URL.")
        return value


class ConversionMetricRequestIn(APIRequestModel):
    """Aggregated traffic and invoice counts for a defined interval."""

    site_id: str = Field(min_length=1, max_length=100)
    camera_id: str | None = Field(default=None, max_length=128)
    interval_start: datetime
    interval_end: datetime
    traffic_count: int = Field(ge=0, le=10_000_000)
    invoice_count: int = Field(ge=0, le=10_000_000)
    average_basket_value: Decimal = Field(
        default=Decimal("0"),
        ge=Decimal("0"),
        le=Decimal("100000000"),
        max_digits=14,
        decimal_places=2,
    )
    currency: str = Field(default="ZAR", min_length=3, max_length=3)

    @field_validator("interval_start", "interval_end", mode="before")
    @classmethod
    def require_timezone_on_interval_times(cls, value: Any) -> Any:
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("Interval timestamps must be valid ISO 8601 timestamps.") from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("Interval timestamps must include an explicit UTC offset.")
            return parsed
        if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Interval timestamps must include an explicit UTC offset.")
        return value

    @model_validator(mode="after")
    def validate_interval(self) -> "ConversionMetricRequestIn":
        if self.interval_end <= self.interval_start:
            raise ValueError("interval_end must be after interval_start.")
        return self

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        normalized = value.upper()
        if not normalized.isalpha():
            raise ValueError("currency must be a three-letter ISO currency code.")
        return normalized
