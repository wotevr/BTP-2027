"""
Danger zone management.

Every write re-compiles the geofence cache, so a zone edited in the dashboard
takes effect on the very next frame with no restart.
"""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database.database import get_db
from app.database.models import DangerZone
from app.schemas.zone import ZoneCreate, ZoneOut, ZoneUpdate
from app.services.geofence_engine import ZoneGeometryError, build_polygon
from app.services.fusion_engine import fusion_engine

router = APIRouter()


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "zone"


def _unique_zone_id(db: Session, base: str) -> str:
    candidate, n = base, 1
    while db.execute(
        select(DangerZone.id).where(DangerZone.zone_id == candidate)
    ).first():
        n += 1
        candidate = f"{base}_{n}"
    return candidate


@router.get("/zones", response_model=list[ZoneOut], summary="List danger zones")
def list_zones(db: Session = Depends(get_db), active_only: bool = False) -> list[ZoneOut]:
    stmt = select(DangerZone).order_by(DangerZone.id)
    if active_only:
        stmt = stmt.where(DangerZone.active.is_(True))
    return [ZoneOut.model_validate(z) for z in db.execute(stmt).scalars()]


@router.post(
    "/zones",
    response_model=ZoneOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a danger zone",
)
def create_zone(payload: ZoneCreate, db: Session = Depends(get_db)) -> ZoneOut:
    # Validate the geometry with the same code the geofence engine will use,
    # so an unusable polygon is rejected here instead of failing silently later.
    try:
        build_polygon(payload.polygon)
    except ZoneGeometryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    zone_id = payload.zone_id or _unique_zone_id(db, _slugify(payload.name))
    if db.execute(select(DangerZone.id).where(DangerZone.zone_id == zone_id)).first():
        raise HTTPException(status_code=409, detail=f"zone_id '{zone_id}' already exists")

    zone = DangerZone(
        zone_id=zone_id,
        name=payload.name,
        zone_type=payload.zone_type.value,
        severity=payload.severity.value,
        polygon=payload.polygon,
        description=payload.description,
        active=payload.active,
    )
    db.add(zone)
    db.commit()
    db.refresh(zone)
    fusion_engine.reload_zones()
    return ZoneOut.model_validate(zone)


@router.put("/zones/{zone_id}", response_model=ZoneOut, summary="Update a danger zone")
def update_zone(zone_id: str, payload: ZoneUpdate, db: Session = Depends(get_db)) -> ZoneOut:
    zone = db.execute(
        select(DangerZone).where(DangerZone.zone_id == zone_id)
    ).scalar_one_or_none()
    if zone is None:
        raise HTTPException(status_code=404, detail=f"zone '{zone_id}' not found")

    data = payload.model_dump(exclude_unset=True)
    if "polygon" in data and data["polygon"] is not None:
        try:
            build_polygon(data["polygon"], zone_id)
        except ZoneGeometryError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    for field_name, value in data.items():
        if value is None and field_name in {"polygon", "name", "active"}:
            continue
        setattr(zone, field_name, getattr(value, "value", value))

    db.commit()
    db.refresh(zone)
    fusion_engine.reload_zones()
    return ZoneOut.model_validate(zone)


@router.get("/site-plan", summary="Static site plan drawn behind the map")
def get_site_plan() -> dict:
    """
    Purely cosmetic background geometry (buildings, gate, haul road).

    It never takes part in any safety calculation - only danger zones do - so
    a missing or unreadable file degrades to an empty plan rather than an error.
    """
    path = settings.site_plan_path
    if not path.exists():
        return {
            "available": False,
            "width_m": settings.SITE_WIDTH_M,
            "depth_m": settings.SITE_DEPTH_M,
            "features": [],
            "note": f"No site plan at {path}. Run scripts/seed_demo_data.py to create one.",
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {
            "available": False,
            "width_m": settings.SITE_WIDTH_M,
            "depth_m": settings.SITE_DEPTH_M,
            "features": [],
            "note": f"Could not read the site plan: {exc}",
        }
    data["available"] = True
    return data


@router.delete("/zones/{zone_id}", summary="Delete a danger zone")
def delete_zone(zone_id: str, db: Session = Depends(get_db)) -> dict:
    zone = db.execute(
        select(DangerZone).where(DangerZone.zone_id == zone_id)
    ).scalar_one_or_none()
    if zone is None:
        raise HTTPException(status_code=404, detail=f"zone '{zone_id}' not found")
    db.delete(zone)
    db.commit()
    fusion_engine.reload_zones()
    return {"deleted": zone_id}
