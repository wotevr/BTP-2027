"""Safety alert schemas."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.database.models import AlertStatus, AlertType, Severity


class Location(BaseModel):
    x: float | None = None
    y: float | None = None


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_id: str
    worker_id: int
    alert_type: AlertType
    severity: Severity
    message: str
    zone_id: str | None = None
    ppe_item: str | None = None
    world_x: float | None = None
    world_y: float | None = None
    timestamp: datetime
    status: AlertStatus
    resolved_at: datetime | None = None


class SafetyEvent(BaseModel):
    """
    The in-memory form of an event, broadcast over WebSocket the moment it is
    raised. Persisted as a SafetyAlert row by the background writer.

    `momentary` separates the two kinds of event:

      * ONGOING CONDITIONS (ZONE_BREACH, PPE_MISSING) stay ACTIVE for as long
        as the condition holds, and resolve when it stops.
      * MOMENTARY FACTS (WORKER_ENTERED_ZONE, WORKER_EXITED_ZONE) happen at one
        instant. They belong in the audit log, not in the open-alerts list, so
        they are written straight to RESOLVED.
    """

    event_id: str
    worker_id: int
    event_type: AlertType
    severity: Severity
    zone_id: str | None = None
    ppe_item: str | None = None
    message: str
    timestamp: datetime
    location: Location = Location()
    momentary: bool = False
