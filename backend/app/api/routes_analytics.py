"""Aggregated site analytics for the dashboard."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.database import get_db
from app.database.models import AlertStatus, SafetyAlert, WorkerAttendance, WorkerStatus
from app.services.alert_service import alert_service
from app.services.geofence_engine import geofence_engine
from app.services.worker_service import worker_service

router = APIRouter()


@router.get("/analytics", summary="Headline metrics and breakdowns")
def analytics(db: Session = Depends(get_db), hours: int = Query(24, ge=1, le=720)) -> dict:
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    total_workers = db.execute(
        select(func.count()).select_from(WorkerAttendance)
    ).scalar_one()

    live = worker_service.all()
    active_live = [w for w in live if w.status is WorkerStatus.ON_SITE]

    # Time on site: prefer the live figure for workers currently tracked.
    db_times = dict(
        db.execute(
            select(WorkerAttendance.worker_id, WorkerAttendance.total_time_seconds)
        ).all()
    )
    for w in live:
        db_times[w.worker_id] = w.total_time_seconds
    times = list(db_times.values())
    avg_time = sum(times) / len(times) if times else 0.0

    incidents_today = db.execute(
        select(func.count())
        .select_from(SafetyAlert)
        .where(
            SafetyAlert.timestamp >= day_start,
            SafetyAlert.alert_type.in_(["ZONE_BREACH", "PPE_MISSING"]),
        )
    ).scalar_one()

    by_type = dict(
        db.execute(
            select(SafetyAlert.alert_type, func.count())
            .where(SafetyAlert.timestamp >= since)
            .group_by(SafetyAlert.alert_type)
        ).all()
    )
    by_severity = dict(
        db.execute(
            select(SafetyAlert.severity, func.count())
            .where(SafetyAlert.timestamp >= since)
            .group_by(SafetyAlert.severity)
        ).all()
    )
    by_zone = dict(
        db.execute(
            select(SafetyAlert.zone_id, func.count())
            .where(
                SafetyAlert.timestamp >= since,
                SafetyAlert.alert_type == "ZONE_BREACH",
                SafetyAlert.zone_id.is_not(None),
            )
            .group_by(SafetyAlert.zone_id)
        ).all()
    )
    by_ppe = dict(
        db.execute(
            select(SafetyAlert.ppe_item, func.count())
            .where(
                SafetyAlert.timestamp >= since,
                SafetyAlert.alert_type == "PPE_MISSING",
                SafetyAlert.ppe_item.is_not(None),
            )
            .group_by(SafetyAlert.ppe_item)
        ).all()
    )

    # Alerts per hour, oldest first, for the trend chart.
    rows = db.execute(
        select(SafetyAlert.timestamp, SafetyAlert.severity).where(
            SafetyAlert.timestamp >= since
        )
    ).all()
    buckets: dict[str, int] = {}
    for ts, _sev in rows:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        key = ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00Z")
        buckets[key] = buckets.get(key, 0) + 1
    timeline = [{"hour": k, "count": v} for k, v in sorted(buckets.items())]

    unresolved_db = db.execute(
        select(func.count())
        .select_from(SafetyAlert)
        .where(SafetyAlert.status == AlertStatus.ACTIVE.value)
    ).scalar_one()

    zone_occupancy = []
    for zone in geofence_engine.zones:
        occupants = [
            w.worker_id for w in active_live if zone.zone_id in (w.zones or [])
        ]
        zone_occupancy.append(
            {
                "zone_id": zone.zone_id,
                "name": zone.name,
                "severity": zone.severity,
                "area_m2": round(zone.area, 1),
                "occupancy": len(occupants),
                "workers": occupants,
                "breaches": by_zone.get(zone.zone_id, 0),
            }
        )

    return {
        "generated_at": now,
        "window_hours": hours,
        "totals": {
            "total_workers": max(total_workers, len(live)),
            "active_workers": len(active_live),
            "active_alerts": len(alert_service.active_alerts),
            "unresolved_alerts_db": unresolved_db,
            "incidents_today": incidents_today,
            "avg_time_on_site_seconds": round(avg_time, 1),
            "zones_configured": len(geofence_engine.zones),
        },
        "alerts_by_type": by_type,
        "alerts_by_severity": by_severity,
        "alerts_by_zone": by_zone,
        "alerts_by_ppe_item": by_ppe,
        "timeline": timeline,
        "zone_occupancy": zone_occupancy,
    }
