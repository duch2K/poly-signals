"""Input signal (spec §3)."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class Signal(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    signal_id: UUID
    market: str = Field(min_length=1)
    size: float = Field(gt=0)          # shares per leg
    price_up: float = Field(gt=0, lt=1)
    price_down: float = Field(gt=0, lt=1)
    lag_seconds: float = Field(ge=0)
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def parse_signal(raw: dict) -> tuple[Signal | None, str]:
    """Return (signal, "") or (None, rejection reason). All fields are required."""
    if not isinstance(raw, dict):
        return None, "invalid_json"
    try:
        return Signal.model_validate(raw), ""
    except ValidationError as e:
        parts = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"]) or "signal"
            parts.append(f"{loc}:{err['type']}")
        return None, "invalid_signal[" + ",".join(parts) + "]"
