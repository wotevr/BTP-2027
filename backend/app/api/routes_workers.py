"""Worker attendance, detail and movement history."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.database import get_db
from app.database.models import LocationTelemetry, SafetyAlert, WorkerAttendance
from app.schemas.alert import AlertOut
from app.schemas.worker import TelemetryPoint, WorkerDetail, WorkerOut
from app.services.alert_service import alert_service
from app.services.worker_service import worker_service

router = APIRouter()


def _live_payload(worker_id: int):
    state = worker_service.get(worker_id)
    if state is None:
        return None
    return worker_service.to_live_worker(
        state, alert_service.active_types_for_worker(worker_id)
    )


@router.get("/workers", response_model=list[WorkerOut], summary="All known workers")
def list_workers(
    db: Session = Depends(get_db),
    status: str | None = Query(None, description="ON_SITE or OFF_SITE"),
    limit: int = Query(200, ge=1, le=1000),
) -> list[WorkerOut]:
    """
    Attendance rows from the database, refreshed with the live in-memory status
    so a worker who is on screen right now is never shown as OFF_SITE just
    because the background writer has not flushed yet.
    """
    stmt = select(WorkerAttendance).order_by(WorkerAttendance.last_seen.desc()).limit(limit)
    rows = list(db.execute(stmt).scalars())

    out: list[WorkerOut] = []
    for row in rows:
        item = WorkerOut.model_validate(row)
        live = worker_service.get(row.worker_id)
        if live is not None:
            item.current_status = live.status
            item.last_seen = live.last_seen
            item.total_time_seconds = live.total_time_seconds
            item.incident_count = live.incident_count
            if live.world_x is not None:
                item.last_world_x, item.last_world_y = live.world_x, live.world_y
        out.append(item)

    # Workers seen this run that have not been flushed to the DB yet.
    known = {r.worker_id for r in rows}
    for live in worker_service.all():
        if live.worker_id in known:
            continue
        out.append(
            WorkerOut(
                worker_id=live.worker_id,
                camera_id=live.camera_id,
                first_seen=live.first_seen,
                last_seen=live.last_seen,
                total_time_seconds=live.total_time_seconds,
                current_status=live.status,
                last_world_x=live.world_x,
                last_world_y=live.world_y,
                incident_count=live.incident_count,
            )
        )

    if status:
        out = [w for w in out if w.current_status.value == status.upper()]
    out.sort(key=lambda w: w.last_seen, reverse=True)
    return out


@router.get("/workers/{worker_id}", response_model=WorkerDetail, summary="One worker")
def get_worker(
    worker_id: int,
    db: Session = Depends(get_db),
    trajectory_minutes: int = Query(30, ge=1, le=1440),
    trajectory_limit: int = Query(500, ge=10, le=5000),
) -> WorkerDetail:
    row = db.get(WorkerAttendance, worker_id) or db.execute(
        select(WorkerAttendance).where(WorkerAttendance.worker_id == worker_id)
    ).scalar_one_or_none()

    live_state = worker_service.get(worker_id)
    if row is None and live_state is None:
        raise HTTPException(status_code=404, detail=f"worker {worker_id} not found")

    if row is not None:
        worker = WorkerOut.model_validate(row)
    else:
        worker = WorkerOut(
            worker_id=live_state.worker_id,
            camera_id=live_state.camera_id,
            first_seen=live_state.first_seen,
            last_seen=live_state.last_seen,
            total_time_seconds=live_state.total_time_seconds,
            current_status=live_state.status,
            last_world_x=live_state.world_x,
            last_world_y=live_state.world_y,
            incident_count=live_state.incident_count,
        )

    if live_state is not None:
        worker.current_status = live_state.status
        worker.last_seen = live_state.last_seen
        worker.total_time_seconds = live_state.total_time_seconds
        worker.incident_count = live_state.incident_count
        if live_state.world_x is not None:
            worker.last_world_x, worker.last_world_y = live_state.world_x, live_state.world_y

    since = datetime.now(timezone.utc) - timedelta(minutes=trajectory_minutes)
    points = list(
        db.execute(
            select(LocationTelemetry)
            .where(
                LocationTelemetry.worker_id == worker_id,
                LocationTelemetry.timestamp >= since,
            )
            .order_by(LocationTelemetry.timestamp.asc())
            .limit(trajectory_limit)
        ).scalars()
    )

    alerts = list(
        db.execute(
            select(SafetyAlert)
            .where(SafetyAlert.worker_id == worker_id)
            .order_by(SafetyAlert.timestamp.desc())
            .limit(50)
        ).scalars()
    )

    return WorkerDetail(
        worker=worker,
        live=_live_payload(worker_id),
        recent_alerts=[AlertOut.model_validate(a) for a in alerts],
        trajectory=[TelemetryPoint.model_validate(p) for p in points],
    )


@router.get(
    "/workers/{worker_id}/trajectory",
    response_model=list[TelemetryPoint],
    summary="Stored movement history in world coordinates",
)
def get_trajectory(
    worker_id: int,
    db: Session = Depends(get_db),
    minutes: int = Query(60, ge=1, le=1440),
    limit: int = Query(1000, ge=10, le=10000),
) -> list[TelemetryPoint]:
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    rows = db.execute(
        select(LocationTelemetry)
        .where(
            LocationTelemetry.worker_id == worker_id,
            LocationTelemetry.timestamp >= since,
        )
        .order_by(LocationTelemetry.timestamp.asc())
        .limit(limit)
    ).scalars()
    return [TelemetryPoint.model_validate(r) for r in rows]
