"""
Seed the database with a demonstrable site.

Creates:
  * four danger zones laid out on a 30 m x 20 m site,
  * a site plan description the dashboard uses to draw the background,
  * a DEMO camera calibration, only if no real one exists yet.

Run it once before the first demo:

    python scripts/seed_demo_data.py

It is idempotent: existing zones are left alone unless you pass --reset.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.database.database import init_db, session_scope  # noqa: E402
from app.database.models import (  # noqa: E402
    DangerZone,
    LocationTelemetry,
    SafetyAlert,
    Severity,
    WorkerAttendance,
    ZoneType,
)
from app.services.demo_simulator import build_demo_transform  # noqa: E402

# Zones are in site metres, on a 30 m (X) by 20 m (Y) site.
DEMO_ZONES = [
    {
        "zone_id": "machinery_01",
        "name": "Heavy Machinery Zone",
        "zone_type": ZoneType.HEAVY_MACHINERY.value,
        "severity": Severity.HIGH.value,
        "polygon": [[12.0, 10.0], [20.0, 10.0], [20.0, 15.0], [12.0, 15.0]],
        "description": "Excavator and crane slewing radius. Spotter required.",
    },
    {
        "zone_id": "excavation_01",
        "name": "Excavation Pit",
        "zone_type": ZoneType.EXCAVATION.value,
        "severity": Severity.CRITICAL.value,
        "polygon": [[21.0, 1.0], [28.0, 2.0], [29.0, 7.0], [22.0, 6.0]],
        "description": "Open trench, 2.4 m deep. Edge protection and harness.",
    },
    {
        "zone_id": "open_edge_01",
        "name": "Open Edge - South Deck",
        "zone_type": ZoneType.OPEN_EDGE.value,
        "severity": Severity.HIGH.value,
        "polygon": [[8.0, 0.0], [22.0, 0.0], [22.0, 2.0], [8.0, 2.0]],
        "description": "Unprotected slab edge. Fall risk.",
    },
    {
        "zone_id": "restricted_01",
        "name": "Restricted Area - Materials Store",
        "zone_type": ZoneType.RESTRICTED_AREA.value,
        "severity": Severity.MEDIUM.value,
        "polygon": [[0.5, 14.0], [6.0, 14.0], [6.0, 19.5], [0.5, 19.5]],
        "description": "Authorised personnel only. Chemical and fuel storage.",
    },
]

# Static furniture drawn on the dashboard map. Purely cosmetic - it carries no
# safety meaning and is never used in any calculation.
SITE_PLAN = {
    "name": "Site B - Tower 3 Podium",
    "width_m": 30.0,
    "depth_m": 20.0,
    "note": "Local site coordinates in metres. X = width, Y = depth. Not GPS.",
    "features": [
        {"type": "building", "label": "Tower 3 Core", "polygon": [[9.0, 5.5], [19.0, 5.5], [19.0, 9.5], [9.0, 9.5]]},
        {"type": "building", "label": "Site Office", "polygon": [[24.0, 15.0], [29.5, 15.0], [29.5, 19.5], [24.0, 19.5]]},
        {"type": "access", "label": "Gate", "polygon": [[0.0, 8.5], [1.2, 8.5], [1.2, 11.5], [0.0, 11.5]]},
        {"type": "path", "label": "Haul Road", "polygon": [[1.2, 9.4], [30.0, 9.4], [30.0, 10.6], [1.2, 10.6]]},
    ],
}


def seed_zones(reset: bool) -> tuple[int, int]:
    created = skipped = 0
    with session_scope() as db:
        if reset:
            db.query(DangerZone).delete()
            db.flush()
        existing = {z.zone_id for z in db.query(DangerZone).all()}
        for spec in DEMO_ZONES:
            if spec["zone_id"] in existing:
                skipped += 1
                continue
            db.add(DangerZone(**spec, active=True))
            created += 1
    return created, skipped


def seed_site_plan(force: bool) -> Path | None:
    path = settings.site_plan_path
    if path.exists() and not force:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(SITE_PLAN, indent=2), encoding="utf-8")
    return path


def seed_calibration(force: bool) -> Path | None:
    """
    Write a DEMO calibration, clearly labelled as such.

    A real deployment replaces this with scripts/calibrate_homography.py. We
    never overwrite an existing file unless asked, so a real calibration is
    safe from this script.
    """
    path = settings.homography_path
    if path.exists() and not force:
        return None
    transform = build_demo_transform()
    payload = transform.to_dict()
    payload["demo"] = True
    payload["note"] = (
        "DEMO CALIBRATION - a synthetic camera looking across a 30m x 20m site. "
        "Replace it by running scripts/calibrate_homography.py against your own "
        "camera before trusting any coordinate."
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def clear_runtime_data() -> None:
    with session_scope() as db:
        db.query(LocationTelemetry).delete()
        db.query(SafetyAlert).delete()
        db.query(WorkerAttendance).delete()


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed demo zones, site plan and calibration.")
    parser.add_argument("--reset", action="store_true", help="delete existing zones first")
    parser.add_argument(
        "--clear-history",
        action="store_true",
        help="also delete all workers, telemetry and alerts",
    )
    parser.add_argument(
        "--force-calibration",
        action="store_true",
        help="overwrite an existing homography.json with the demo one",
    )
    args = parser.parse_args()

    init_db()
    print(f"Database: {settings.sqlalchemy_url}")

    if args.clear_history:
        clear_runtime_data()
        print("Cleared workers, telemetry and alerts.")

    created, skipped = seed_zones(args.reset)
    print(f"Danger zones: {created} created, {skipped} already present")
    for spec in DEMO_ZONES:
        print(f"   - {spec['name']:<38} {spec['severity']:<8} {spec['zone_id']}")

    plan = seed_site_plan(args.reset)
    print(f"Site plan: {'written to ' + str(plan) if plan else 'already present, left alone'}")

    calib = seed_calibration(args.force_calibration)
    if calib:
        print(f"Calibration: DEMO calibration written to {calib}")
        print("             Replace it with scripts/calibrate_homography.py for real use.")
    else:
        print("Calibration: existing file left alone (use --force-calibration to replace)")

    print("\nReady. Start the backend and switch DEMO MODE on in the dashboard.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
