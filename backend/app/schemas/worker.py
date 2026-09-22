"""Worker-facing schemas (live state, attendance, telemetry)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.database.models import WorkerStatus
from app.schemas.alert import AlertOut
from app.schemas.detection import PPEStatus


class Position(BaseModel):
    x: float
    y: float


class LiveWorker(BaseModel):
    """A worker as the dashboard sees them right now."""

    id: int
    pixel_position: Position
    # Null when spatial calibration is unavailable.
    world_position: Position | None = None
    ppe: PPEStatus
    ppe_labels: dict[str, str] = {}
    zones: list[str] = []
    active_alerts: list[str] = []
    state: str = "SAFE"  # SAFE | WARNING | DANGER
    last_seen: datetime
    confidence: float = 1.0


class WorkerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    worker_id: int
    camera_id: str
    first_seen: datetime
    last_seen: datetime
    total_time_seconds: float
    current_status: WorkerStatus
    last_world_x: float | None = None
    last_world_y: float | None = None
    incident_count: int


class TelemetryPoint(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    timestamp: datetime
    pixel_x: float
    pixel_y: float
    world_x: float | None = None
    world_y: float | None = None
    zone_id: str | None = None


class WorkerDetail(BaseModel):
    worker: WorkerOut
    live: LiveWorker | None = None
    recent_alerts: list[AlertOut] = []
    trajectory: list[TelemetryPoint] = []
