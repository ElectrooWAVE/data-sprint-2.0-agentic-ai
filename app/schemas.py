"""Request models for the API (responses are plain JSON dicts built by the engine wrapper)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

CUSTOM_FIELDS = ("supplier", "category", "quantity", "round", "date", "supplier_message")


class DecideRequest(BaseModel):
    """Either {thread_id[, source]} to look up a known thread, or a full custom thread."""

    thread_id: str | None = Field(None, max_length=64)
    source: Literal["sample", "real"] | None = None
    supplier: str | None = Field(None, max_length=200)
    category: str | None = Field(None, max_length=100)
    quantity: int | None = Field(None, ge=1, le=10_000_000)
    round: int | None = Field(None, ge=1, le=100)
    date: str | None = Field(None, max_length=10, description="ISO date YYYY-MM-DD")
    our_last_counter: Decimal | None = Field(None, ge=0)
    supplier_message: str | None = Field(None, max_length=20_000)

    def is_custom(self) -> bool:
        return any(getattr(self, f) is not None for f in CUSTOM_FIELDS)

    def missing_custom_fields(self) -> list[str]:
        return [f for f in CUSTOM_FIELDS if getattr(self, f) is None]


class BatchChunkRequest(BaseModel):
    """Stateless batch chunk payload (up to 250 rows per request)."""

    rows: list[dict[str, Any]] = Field(..., min_length=1, max_length=250)
