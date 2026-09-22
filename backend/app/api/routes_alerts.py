"""Safety alert history and lifecycle."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.database.database import get_db
from app.database.models import AlertStatus, SafetyAlert
from app.schemas.alert import AlertOut
from app.services.alert_service import alert_service

router = APIRouter()


@router.get("/alerts", response_model=list[AlertOut], summary="Alert history")
def list_alerts(
    db: Session = Depends(get_db),
    status: str | None = Query(None, description="ACTIVE or RESOLVED"),
    alert_type: str | None = Query(None),
    severity: str | None = Query(None),
    worker_id: int | None = Query(None),
    hours: int = Query(24, ge=1, le=720),
    limit: int = Query(100, ge=1, le=1000),
) -> list[AlertOut]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    stmt = select(SafetyAlert).where(SafetyAlert.timestamp >= since)

    if status:
        stmt = stmt.where(SafetyAlert.status == status.upper())
    if alert_type:
        stmt = stmt.where(SafetyAlert.alert_type == alert_type.upper())
    if severity:
        stmt = stmt.where(SafetyAlert.severity == severity.upper())
    if worker_id is not None:
        stmt = stmt.where(SafetyAlert.worker_id == worker_id)

    stmt = stmt.order_by(SafetyAlert.timestamp.desc()).limit(limit)
    return [AlertOut.model_validate(r) for r in db.execute(stmt).scalars()]


@router.get("/alerts/active", summary="Alerts currently open, straight from memory")
def active_alerts() -> list[dict]:
    """
    Read from the alert engine rather than the database so this reflects the
    live state with no dependency on the background writer having caught up.
    """
    events = [a.to_event().model_dump(mode="json") for a in alert_service.active_alerts]
    events.sort(key=lambda e: e["timestamp"], reverse=True)
    return events


@router.post("/alerts/{event_id}/resolve", summary="Acknowledge and close an alert")
def resolve_alert(event_id: str, db: Session = Depends(get_db)) -> dict:
    now = datetime.now(timezone.utc)
    result = db.execute(
        update(SafetyAlert)
        .where(SafetyAlert.event_id == event_id, SafetyAlert.status == AlertStatus.ACTIVE.value)
        .values(status=AlertStatus.RESOLVED.value, resolved_at=now)
    )
    db.commit()

    # Also drop it from the in-memory engine, otherwise the next frame that
    # re-confirms the condition would keep it open on the dashboard.
    removed = alert_service.resolve_by_event_id(event_id, now) is not None

    if not result.rowcount and not removed:
        raise HTTPException(status_code=404, detail=f"no active alert {event_id}")
    return {"event_id": event_id, "status": AlertStatus.RESOLVED.value, "resolved_at": now}
