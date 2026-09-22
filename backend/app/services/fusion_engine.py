"""
THE FUSION ENGINE - the core contribution of BTP-2027.

Upstream answers "who is this worker and what PPE are they wearing".
This module answers "where are they standing, and is that safe".

Every detection frame - real or simulated - travels exactly this path:

    DetectionFrame (normalised)
        |
        v
    foot point          bottom-centre of the bounding box
        |
        v
    homography          pixel -> local site metres
        |
        v
    geofence            point-in-polygon against active danger zones
        |
        v
    safety events       zone breaches + PPE violations, debounced
        |
        +--> queued to SQLite (never blocking)
        |
        +--> broadcast over WebSocket

Demo mode uses the same function. That is deliberate: what you see in the demo
is the real engine, only the source of the detections differs.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings
from app.core.websocket_manager import manager
from app.database.database import session_scope
from app.database.models import AlertStatus, AlertType, DangerZone, WorkerStatus
from app.schemas.alert import SafetyEvent
from app.schemas.detection import DetectionFrame, IngestionResponse
from app.services.alert_service import alert_service
from app.services.geofence_engine import geofence_engine
from app.services.homography import HomographyError, calibration
from app.services.persistence import (
    AlertRecord,
    AttendanceRecord,
    ResolveRecord,
    TelemetryRecord,
    db_writer,
)
from app.services.worker_service import worker_service

log = logging.getLogger(__name__)

# Alert types that count as a real incident against a worker.
INCIDENT_TYPES = {AlertType.ZONE_BREACH, AlertType.PPE_MISSING}


class FusionEngine:
    def __init__(self) -> None:
        self._last_broadcast = 0.0
        self._lock = asyncio.Lock()
        self.frames_processed = 0
        self.last_frame_at: datetime | None = None
        self._fps_window: list[float] = []
        self.demo_active = False
        self.last_source: str = "live"
        self.last_camera_id: str = settings.CAMERA_ID

    # --------------------------------------------------------------- zones
    def reload_zones(self) -> list[str]:
        """Re-read danger zones from the database into the geofence cache."""
        with session_scope() as db:
            rows = db.query(DangerZone).filter(DangerZone.active.is_(True)).all()
            skipped = geofence_engine.load(rows)
        log.info(
            "Loaded %d active danger zone(s)%s",
            len(geofence_engine.zones),
            f", skipped {skipped}" if skipped else "",
        )
        return skipped

    # ------------------------------------------------------------ ingestion
    async def process_frame(self, frame: DetectionFrame) -> IngestionResponse:
        """
        Process one frame end to end.

        Serialised with a lock: the alert state machine and the geofence state
        machine are not re-entrant, and two sources (a live camera and the demo
        simulator) could otherwise interleave.
        """
        async with self._lock:
            return await self._process(frame)

    async def _process(self, frame: DetectionFrame) -> IngestionResponse:
        now = frame.timestamp
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        monotonic_now = time.monotonic()

        transform = calibration.transform
        events: list[SafetyEvent] = []

        for det in frame.workers:
            # ---------------------------------------------------- foot point
            u, v = det.foot_point

            # ------------------------------------------------- pixel -> world
            world: tuple[float, float] | None = None
            if transform is not None:
                try:
                    su, sv = transform.scale_pixel(
                        u, v, frame.frame_width, frame.frame_height
                    )
                    world = transform.pixel_to_world(su, sv)
                except HomographyError as exc:
                    # A point beyond the horizon has no ground position. The
                    # worker stays visible in pixel space; only the map entry
                    # is skipped for this frame.
                    log.debug("worker %s: %s", det.track_id, exc)
                    world = None

            state = worker_service.observe(
                worker_id=det.track_id,
                camera_id=frame.camera_id,
                pixel=(u, v),
                world=world,
                ppe=det.ppe,
                confidence=det.confidence,
                now=now,
            )

            # ------------------------------------------------------ geofence
            zone_ids: list[str] = []
            if world is not None:
                inside, transitions = geofence_engine.evaluate(
                    det.track_id, world[0], world[1], monotonic_now
                )
                zone_ids = [z.zone_id for z in inside]
                for t in transitions:
                    if t.event == "ENTER":
                        events += alert_service.zone_entered(
                            det.track_id, t.zone.zone_id, t.zone.name,
                            t.zone.severity, world, now,
                        )
                    else:
                        events += alert_service.zone_exited(
                            det.track_id, t.zone.zone_id, t.zone.name,
                            t.dwell_seconds, world, now,
                        )
                # Keep the breach alive for as long as the worker is still in
                # the zone, so the auto-resolve timeout only fires when the
                # evidence actually stops arriving.
                alert_service.confirm_zone_presence(det.track_id, zone_ids, world, now)
            worker_service.set_zones(det.track_id, zone_ids)

            # ----------------------------------------------------------- PPE
            missing = det.ppe.missing_items(settings.required_ppe)
            events += alert_service.evaluate_ppe(det.track_id, missing, world or (None, None), now)

            # ----------------------------------------------------- telemetry
            if worker_service.should_sample_telemetry(det.track_id, now):
                db_writer.submit(
                    TelemetryRecord(
                        worker_id=det.track_id,
                        timestamp=now,
                        pixel_x=u,
                        pixel_y=v,
                        world_x=world[0] if world else None,
                        world_y=world[1] if world else None,
                        zone_id=zone_ids[0] if zone_ids else None,
                    )
                )
                db_writer.submit(self._attendance_record(state))

        # ------------------------------------------ incidents + persistence
        for event in events:
            if event.event_type in INCIDENT_TYPES:
                worker_service.record_incident(event.worker_id)
            db_writer.submit(
                AlertRecord(
                    event_id=event.event_id,
                    worker_id=event.worker_id,
                    alert_type=event.event_type.value,
                    severity=event.severity.value,
                    message=event.message,
                    zone_id=event.zone_id,
                    ppe_item=event.ppe_item,
                    world_x=event.location.x,
                    world_y=event.location.y,
                    timestamp=event.timestamp,
                    # A momentary fact is already over when it is written.
                    status=(
                        AlertStatus.RESOLVED.value
                        if event.momentary
                        else AlertStatus.ACTIVE.value
                    ),
                    resolved_at=event.timestamp if event.momentary else None,
                )
            )

        # -------------------------------- time out conditions no longer seen
        alert_service.tick(now)
        for resolved in alert_service.drain_resolved():
            db_writer.submit(ResolveRecord(resolved.event_id, resolved.resolved_at))

        self._retire_absent_workers(now)

        # ------------------------------------------------------- bookkeeping
        self.frames_processed += 1
        self.last_frame_at = now
        self.last_source = frame.source
        self.last_camera_id = frame.camera_id
        self._track_fps(monotonic_now)

        # New events are pushed immediately; routine state respects the cap.
        await self._maybe_broadcast(force=bool(events), events=events)

        return IngestionResponse(
            success=True,
            processed_workers=len(frame.workers),
            alerts_generated=len(events),
            spatial_calibration=calibration.available,
            detail=None if calibration.available else calibration.error,
        )

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _attendance_record(state) -> AttendanceRecord:
        return AttendanceRecord(
            worker_id=state.worker_id,
            camera_id=state.camera_id,
            first_seen=state.first_seen,
            last_seen=state.last_seen,
            total_time_seconds=state.total_time_seconds,
            current_status=state.status.value,
            last_world_x=state.world_x,
            last_world_y=state.world_y,
            incident_count=state.incident_count,
        )

    def _retire_absent_workers(self, now: datetime) -> None:
        """
        A worker who has not been seen for ABSENT_AFTER_SECONDS is treated as
        having left. Their open alerts are closed and their zone membership is
        forgotten, so they do not stay permanently 'inside' a danger zone.
        """
        for worker_id in worker_service.gone_absent(now):
            state = worker_service.get(worker_id)
            if state is None or state.status is WorkerStatus.ON_SITE:
                continue
            if not geofence_engine.inside_zone_ids(worker_id) and not alert_service.active_for_worker(worker_id):
                continue
            geofence_engine.forget_worker(worker_id)
            alert_service.forget_worker(worker_id, now)
            for resolved in alert_service.drain_resolved():
                db_writer.submit(ResolveRecord(resolved.event_id, resolved.resolved_at))
            db_writer.submit(self._attendance_record(state))
            log.info("Worker #%s marked off site", worker_id)

    def _track_fps(self, now: float) -> None:
        self._fps_window.append(now)
        cutoff = now - 3.0
        while self._fps_window and self._fps_window[0] < cutoff:
            self._fps_window.pop(0)

    @property
    def fps(self) -> float:
        if len(self._fps_window) < 2:
            return 0.0
        span = self._fps_window[-1] - self._fps_window[0]
        return round((len(self._fps_window) - 1) / span, 1) if span > 0 else 0.0

    # ------------------------------------------------------------ broadcast
    async def _maybe_broadcast(
        self, force: bool = False, events: list[SafetyEvent] | None = None
    ) -> None:
        now = time.monotonic()
        min_gap = 1.0 / max(settings.BROADCAST_MAX_FPS, 1.0)
        if not force and (now - self._last_broadcast) < min_gap:
            return
        self._last_broadcast = now
        if manager.client_count == 0:
            return
        await manager.broadcast(self.site_state(new_events=events))

    # ----------------------------------------------------------- site state
    def site_state(self, new_events: list[SafetyEvent] | None = None) -> dict[str, Any]:
        """The full snapshot the dashboard renders. Also the WebSocket payload."""
        now = datetime.now(timezone.utc)
        workers = []
        for state in worker_service.active(now):
            types = alert_service.active_types_for_worker(state.worker_id)
            workers.append(worker_service.to_live_worker(state, types).model_dump(mode="json"))

        alerts = [a.to_event().model_dump(mode="json") for a in alert_service.active_alerts]
        alerts.sort(key=lambda a: a["timestamp"], reverse=True)

        zones = [
            {
                "zone_id": z.zone_id,
                "name": z.name,
                "zone_type": z.zone_type,
                "severity": z.severity,
                "polygon": [[round(x, 3), round(y, 3)] for x, y in z.polygon.exterior.coords],
                "description": z.description,
                "occupancy": sum(1 for w in workers if z.zone_id in w["zones"]),
            }
            for z in geofence_engine.zones
        ]

        return {
            "type": "site_state",
            "timestamp": now,
            "source": "demo" if self.demo_active else self.last_source,
            "demo_mode": self.demo_active,
            "camera_id": self.last_camera_id,
            "fps": self.fps,
            "frames_processed": self.frames_processed,
            "calibration": calibration.status(),
            "site": {"width_m": settings.SITE_WIDTH_M, "depth_m": settings.SITE_DEPTH_M},
            "workers": workers,
            "alerts": alerts,
            "zones": zones,
            "new_events": [e.model_dump(mode="json") for e in (new_events or [])],
        }

    def reset(self) -> None:
        worker_service.reset()
        alert_service.reset()
        geofence_engine.reset()
        self.frames_processed = 0
        self._fps_window.clear()


fusion_engine = FusionEngine()
