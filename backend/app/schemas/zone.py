"""Danger zone request/response schemas."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.database.models import Severity, ZoneType


def _validate_polygon(v: list) -> list[list[float]]:
    if not isinstance(v, list) or len(v) < 3:
        raise ValueError("polygon needs at least 3 vertices")
    cleaned: list[list[float]] = []
    for i, pt in enumerate(v):
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            raise ValueError(f"vertex {i} must be [x, y]")
        try:
            x, y = float(pt[0]), float(pt[1])
        except (TypeError, ValueError):
            raise ValueError(f"vertex {i} has non-numeric coordinates") from None
        cleaned.append([x, y])

    # Reject a polygon whose vertices are all on one line: Shapely would build
    # a zero-area geometry that can never contain a worker.
    area2 = 0.0
    for i in range(len(cleaned)):
        x1, y1 = cleaned[i]
        x2, y2 = cleaned[(i + 1) % len(cleaned)]
        area2 += x1 * y2 - x2 * y1
    if abs(area2) < 1e-9:
        raise ValueError("polygon is degenerate (zero area / collinear vertices)")
    return cleaned


class ZoneBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    zone_type: ZoneType = ZoneType.OTHER
    severity: Severity = Severity.MEDIUM
    polygon: list[list[float]] = Field(..., description="[[x,y],...] in metres")
    description: str = ""
    active: bool = True

    @field_validator("polygon")
    @classmethod
    def _poly(cls, v):
        return _validate_polygon(v)


class ZoneCreate(ZoneBase):
    zone_id: str | None = Field(
        None, description="Stable slug. Auto-generated from the name when omitted."
    )


class ZoneUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    zone_type: ZoneType | None = None
    severity: Severity | None = None
    polygon: list[list[float]] | None = None
    description: str | None = None
    active: bool | None = None

    @field_validator("polygon")
    @classmethod
    def _poly(cls, v):
        return None if v is None else _validate_polygon(v)


class ZoneOut(ZoneBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    zone_id: str
    created_at: datetime
    updated_at: datetime
