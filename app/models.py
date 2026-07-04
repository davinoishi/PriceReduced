"""Database models: monitored items and their price history."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite stores naive; we keep everything in UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def current_month() -> str:
    """Calendar month key (UTC) used to bucket LLM usage for the monthly cap."""
    return utcnow().strftime("%Y-%m")


# Check outcome statuses.
STATUS_OK = "ok"
STATUS_NO_PRICE = "no_price"
STATUS_BLOCKED = "blocked"
STATUS_ERROR = "error"


class Item(SQLModel, table=True):
    """A product URL the user is tracking."""

    id: int | None = Field(default=None, primary_key=True)
    url: str = Field(index=True, unique=True)
    title: str | None = None
    image_url: str | None = None
    currency: str | None = None
    target_price: float | None = None
    interval_minutes: int = 1440
    active: bool = True

    # How the price was last found, cached for reference/optimization.
    extraction_method: str | None = None
    extraction_hint: str | None = None

    # Denormalized latest-check snapshot for a cheap dashboard list view.
    last_price: float | None = None
    last_status: str | None = None
    last_checked_at: datetime | None = None
    next_check_at: datetime | None = None

    created_at: datetime = Field(default_factory=utcnow)


class PricePoint(SQLModel, table=True):
    """One check result. Kept for history (retained until the item is removed)."""

    id: int | None = Field(default=None, primary_key=True)
    item_id: int = Field(index=True, foreign_key="item.id")
    price: float | None = None
    currency: str | None = None
    method_used: str | None = None
    http_status: int | None = None
    ok: bool = False
    status: str = STATUS_ERROR
    raw_value: str | None = None
    checked_at: datetime = Field(default_factory=utcnow, index=True)


class LlmCall(SQLModel, table=True):
    """One OpenRouter fallback call, for usage tracking + the monthly cap."""

    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)
    month: str = Field(default_factory=current_month, index=True)
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    ok: bool = False  # whether the call yielded a price
    item_id: int | None = Field(default=None, foreign_key="item.id")
