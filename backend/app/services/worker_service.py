"""
Live worker state and the attendance policy.

The authoritative "who is on site right now" lives in memory: it is read and
written many times per second and must never wait on SQLite. The database gets
a summary row per worker, updated by the background writer.

ATTENDANCE POLICY (explain this one in the viva)
------------------------------------------------
A worker is not "absent" the instant a frame misses them. Detectors drop a box
for all sorts of innocent reasons: someone walks behind a pillar, a truck
passes, the box confidence dips under threshold for three frames.

So time on site is accumulated INCREMENTALLY between consecutive sightings:

    gap = now - last_seen
    if gap <= PRESENCE_GAP_SECONDS:   total_time += gap     # continuous presence
    else:                             total_time += 0       # they were away

The gap itself is never counted when it is large. This means a worker who
leaves for lunch and comes back keeps one attendance record, with the lunch
break correctly excluded, and no assumption is made about what happened while
they were invisible.

Status flips to OFF_SITE once nothing has been seen for ABSENT_AFTER_SECONDS.

A caveat worth stating openly: identity here is the tracker track id. If
BoT-SORT loses a worker and re-acquires them with a new id, they appear as a
new worker. Re-identification across track breaks is out of scope for this
project and is the obvious next extension.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.database.models import WorkerStatus
from app.schemas.detection import PPEStatus
from app.schemas.worker import LiveWorker, Position

log = logging.getLogger(__name__)


@dataclass
class LiveWorkerState:
    worker_id: int
    camera_id: str
    first_seen: datetime
    last_seen: datetime
    pixel_x: float
    pixel_y: float
    world_x: float | None = None
    world_y: float | None = None
    ppe: PPEStatus = field(default_factory=PPEStatus)
    confidence: float = 1.0
    zones: list[str] = field(default_factory=list)
    total_time_seconds: float = 0.0
    incident_count: int = 0
    # Last time a telemetry row was emitted for this worker.
    last_telemetry_at: datetime | None = None
    # True until the attendance row has been created in the database.
    is_new: bool = True
    dirty: bool = True

    @property
    def status(self) -> WorkerStatus:
        age = (datetime.now(timezone.utc) - self.last_seen).total_seconds()
        return WorkerStatus.ON_SITE if age <= settings.ABSENT_AFTER_SECONDS else WorkerStatus.OFF_SITE


class WorkerService:
    """In-memory registry of every worker the system has seen this run."""

    def __init__(self) -> None:
        self._workers: dict[int, LiveWorkerState] = {}

    # ------------------------------------------------------------- queries
    def get(self, worker_id: int) -> LiveWorkerState | None:
        return self._workers.get(worker_id)

    def all(self) -> list[LiveWorkerState]:
        return list(self._workers.values())

    def active(self, now: datetime | None = None) -> list[LiveWorkerState]:
        now = now or datetime.now(timezone.utc)
        limit = timedelta(seconds=settings.ABSENT_AFTER_SECONDS)
        return [w for w in self._workers.values() if now - w.last_seen <= limit]

    # ---------------------------------------------------------- observation
    def observe(
        self,
        worker_id: int,
        camera_id: str,
        pixel: tuple[float, float],
        world: tuple[float, float] | None,
        ppe: PPEStatus,
        confidence: float,
        now: datetime,
    ) -> LiveWorkerState:
        """Record one sighting and apply the presence policy."""
        state = self._workers.get(worker_id)

        if state is None:
            state = LiveWorkerState(
                worker_id=worker_id,
                camera_id=camera_id,
                first_seen=now,
                last_seen=now,
                pixel_x=pixel[0],
                pixel_y=pixel[1],
            )
            self._workers[worker_id] = state
            log.info("New worker on site: #%s", worker_id)
        else:
            gap = (now - state.last_seen).total_seconds()
            # Only continuous presence counts. A long gap means they were away.
            if 0 < gap <= settings.PRESENCE_GAP_SECONDS:
                state.total_time_seconds += gap
            state.last_seen = now

        state.pixel_x, state.pixel_y = pixel
        if world is not None:
            state.world_x, state.world_y = world
        state.ppe = ppe
        state.confidence = confidence
        state.camera_id = camera_id
        state.dirty = True
        return state

    def set_zones(self, worker_id: int, zone_ids: list[str]) -> None:
        state = self._workers.get(worker_id)
        if state is not None:
            state.zones = zone_ids

    def record_incident(self, worker_id: int, count: int = 1) -> None:
        state = self._workers.get(worker_id)
        if state is not None:
            state.incident_count += count
            state.dirty = True

    def should_sample_telemetry(self, worker_id: int, now: datetime) -> bool:
        """Rate-limit telemetry rows per worker, not per frame."""
        state = self._workers.get(worker_id)
        if state is None:
            return False
        last = state.last_telemetry_at
        if last is None or (now - last).total_seconds() >= settings.TELEMETRY_SAMPLE_INTERVAL:
            state.last_telemetry_at = now
            return True
        return False

    def gone_absent(self, now: datetime | None = None) -> list[int]:
        """Workers whose last sighting is older than the absence threshold."""
        now = now or datetime.now(timezone.utc)
        limit = settings.ABSENT_AFTER_SECONDS
        return [
            w.worker_id
            for w in self._workers.values()
            if (now - w.last_seen).total_seconds() > limit
        ]

    # ------------------------------------------------------------ dashboard
    def to_live_worker(
        self, state: LiveWorkerState, active_alert_types: list[str]
    ) -> LiveWorker:
        if "ZONE_BREACH" in active_alert_types:
            ui_state = "DANGER"
        elif active_alert_types:
            ui_state = "WARNING"
        else:
            ui_state = "SAFE"

        world = (
            Position(x=round(state.world_x, 2), y=round(state.world_y, 2))
            if state.world_x is not None and state.world_y is not None
            else None
        )
        return LiveWorker(
            id=state.worker_id,
            pixel_position=Position(x=round(state.pixel_x, 1), y=round(state.pixel_y, 1)),
            world_position=world,
            ppe=state.ppe,
            ppe_labels=state.ppe.as_labels(),
            zones=list(state.zones),
            active_alerts=active_alert_types,
            state=ui_state,
            last_seen=state.last_seen,
            confidence=round(state.confidence, 3),
        )

    def reset(self) -> None:
        self._workers.clear()


worker_service = WorkerService()
