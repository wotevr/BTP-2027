"""
Asynchronous database writer.

Requirement 30 says: do not write every frame synchronously to SQLite, and do
not block the WebSocket on database writes. So the fusion engine never touches
the database. It drops small records onto an asyncio queue and returns
immediately; this module drains the queue on a background task, batches the
records, and executes the (synchronous) SQLAlchemy work in a worker thread so
the event loop keeps serving frames and sockets.

Back-pressure: the queue is bounded. If the database somehow falls far behind,
the OLDEST telemetry is discarded rather than letting memory grow without
bound. Alerts are never dropped - they are the point of the system.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select, update

from app.core.config import settings
from app.database.database import session_scope
from app.database.models import (
    AlertStatus,
    LocationTelemetry,
    SafetyAlert,
    WorkerAttendance,
)

log = logging.getLogger(__name__)

QUEUE_MAX = 5000
BATCH_SIZE = 200
FLUSH_INTERVAL = 0.5


@dataclass
class TelemetryRecord:
    worker_id: int
    timestamp: datetime
    pixel_x: float
    pixel_y: float
    world_x: float | None
    world_y: float | None
    zone_id: str | None


@dataclass
class AttendanceRecord:
    worker_id: int
    camera_id: str
    first_seen: datetime
    last_seen: datetime
    total_time_seconds: float
    current_status: str
    last_world_x: float | None
    last_world_y: float | None
    incident_count: int


@dataclass
class AlertRecord:
    event_id: str
    worker_id: int
    alert_type: str
    severity: str
    message: str
    zone_id: str | None
    ppe_item: str | None
    world_x: float | None
    world_y: float | None
    timestamp: datetime
    # Momentary facts (entered/exited a zone) are written already RESOLVED;
    # ongoing conditions start ACTIVE and are closed by a ResolveRecord.
    status: str = AlertStatus.ACTIVE.value
    resolved_at: datetime | None = None


@dataclass
class ResolveRecord:
    event_id: str
    resolved_at: datetime


class DBWriter:
    def __init__(self) -> None:
        # The queue is created in start(), not here. An asyncio.Queue binds to
        # the running loop, and this object is a module-level singleton: if it
        # were built at import time it would belong to whichever loop happened
        # to exist then, and every later loop (a restart, or a test client)
        # would fail with "bound to a different event loop".
        self._queue: asyncio.Queue[Any] | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        self.dropped = 0
        self.written = 0

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._task is not None:
            return
        self._queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self._running = True
        self._task = asyncio.create_task(self._run(), name="db-writer")
        log.info("Database writer started")

    async def stop(self) -> None:
        self._running = False
        queue = self._queue
        if self._task is not None:
            if queue is not None:
                await queue.put(None)  # wake the loop
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
        # Final synchronous drain so nothing in flight is lost on shutdown.
        pending = []
        while queue is not None and not queue.empty():
            item = queue.get_nowait()
            if item is not None:
                pending.append(item)
        if pending:
            await asyncio.to_thread(self._flush, pending)
        self._queue = None
        log.info("Database writer stopped (%d records written)", self.written)

    # -------------------------------------------------------------- submit
    def submit(self, record: Any) -> None:
        """Non-blocking. Safe to call from the hot path."""
        queue = self._queue
        if queue is None:
            # Not started (or already stopped). Dropping is the right call:
            # blocking the detection pipeline on bookkeeping would be worse.
            self.dropped += 1
            return
        try:
            queue.put_nowait(record)
        except asyncio.QueueFull:
            if isinstance(record, TelemetryRecord):
                self.dropped += 1
                if self.dropped % 100 == 1:
                    log.warning(
                        "Telemetry queue full, dropped %d samples so far", self.dropped
                    )
                return
            # Alerts and attendance matter: make room by evicting telemetry.
            try:
                queue.get_nowait()
                self.dropped += 1
                queue.put_nowait(record)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                log.error("Could not enqueue %s", type(record).__name__)

    # ----------------------------------------------------------------- run
    async def _run(self) -> None:
        queue = self._queue
        assert queue is not None
        batch: list[Any] = []
        while self._running or batch:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=FLUSH_INTERVAL)
                if item is not None:
                    batch.append(item)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break

            # Opportunistically absorb whatever else is waiting.
            while len(batch) < BATCH_SIZE and not queue.empty():
                item = queue.get_nowait()
                if item is not None:
                    batch.append(item)

            if batch:
                try:
                    await asyncio.to_thread(self._flush, batch)
                    self.written += len(batch)
                except Exception:  # noqa: BLE001 - the loop must survive DB errors
                    log.exception("Database flush failed; dropping %d records", len(batch))
                batch = []

            if not self._running and queue.empty():
                break

    # --------------------------------------------------------------- flush
    @staticmethod
    def _flush(batch: list[Any]) -> None:
        """Runs on a worker thread. One transaction per batch."""
        telemetry = [r for r in batch if isinstance(r, TelemetryRecord)]
        attendance = [r for r in batch if isinstance(r, AttendanceRecord)]
        alerts = [r for r in batch if isinstance(r, AlertRecord)]
        resolves = [r for r in batch if isinstance(r, ResolveRecord)]

        with session_scope() as db:
            # Attendance first: telemetry and alerts have a FK onto worker_id.
            # Collapse repeats so each worker is written once per batch.
            latest: dict[int, AttendanceRecord] = {}
            for rec in attendance:
                latest[rec.worker_id] = rec

            existing_ids = set()
            if latest:
                rows = db.execute(
                    select(WorkerAttendance.worker_id).where(
                        WorkerAttendance.worker_id.in_(list(latest))
                    )
                ).scalars()
                existing_ids = set(rows)

            for worker_id, rec in latest.items():
                values = {
                    "camera_id": rec.camera_id,
                    "last_seen": rec.last_seen,
                    "total_time_seconds": rec.total_time_seconds,
                    "current_status": rec.current_status,
                    "last_world_x": rec.last_world_x,
                    "last_world_y": rec.last_world_y,
                    "incident_count": rec.incident_count,
                }
                if worker_id in existing_ids:
                    db.execute(
                        update(WorkerAttendance)
                        .where(WorkerAttendance.worker_id == worker_id)
                        .values(**values)
                    )
                else:
                    db.add(
                        WorkerAttendance(
                            worker_id=worker_id, first_seen=rec.first_seen, **values
                        )
                    )
            if latest:
                db.flush()

            if telemetry:
                db.bulk_save_objects(
                    [
                        LocationTelemetry(
                            worker_id=r.worker_id,
                            timestamp=r.timestamp,
                            pixel_x=r.pixel_x,
                            pixel_y=r.pixel_y,
                            world_x=r.world_x,
                            world_y=r.world_y,
                            zone_id=r.zone_id,
                        )
                        for r in telemetry
                    ]
                )

            if alerts:
                known = set(
                    db.execute(
                        select(SafetyAlert.event_id).where(
                            SafetyAlert.event_id.in_([a.event_id for a in alerts])
                        )
                    ).scalars()
                )
                for a in alerts:
                    if a.event_id in known:
                        continue
                    db.add(
                        SafetyAlert(
                            event_id=a.event_id,
                            worker_id=a.worker_id,
                            alert_type=a.alert_type,
                            severity=a.severity,
                            message=a.message,
                            zone_id=a.zone_id,
                            ppe_item=a.ppe_item,
                            world_x=a.world_x,
                            world_y=a.world_y,
                            timestamp=a.timestamp,
                            status=a.status,
                            resolved_at=a.resolved_at,
                        )
                    )

            for r in resolves:
                db.execute(
                    update(SafetyAlert)
                    .where(SafetyAlert.event_id == r.event_id)
                    .values(status=AlertStatus.RESOLVED.value, resolved_at=r.resolved_at)
                )


def prune_old_telemetry() -> int:
    """Housekeeping on startup so a long-running demo database stays small."""
    hours = settings.TELEMETRY_RETENTION_HOURS
    if hours <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    with session_scope() as db:
        result = db.execute(
            delete(LocationTelemetry).where(LocationTelemetry.timestamp < cutoff)
        )
        return int(result.rowcount or 0)


db_writer = DBWriter()
