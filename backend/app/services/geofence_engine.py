"""
Geofencing over the site ground plane.

Danger zones are Shapely polygons expressed in the SAME local site metres that
`homography.pixel_to_world` produces. A worker foot point is a Shapely Point,
and zone membership is a point-in-polygon test.

CONTAINMENT SEMANTICS
---------------------
We use `covers()`, not `contains()`.

`contains()` treats the boundary as outside: a worker standing exactly on the
painted edge of an excavation would be reported safe. `covers()` includes the
boundary, so touching the edge already counts as being in the zone. For a
safety system the conservative choice is the correct one.

The polygons are wrapped in Shapely prepared geometries, which pre-index the
edges so repeated point queries stay cheap at video frame rates.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from shapely.geometry import Point, Polygon
from shapely.prepared import prep
from shapely.validation import explain_validity

log = logging.getLogger(__name__)


class ZoneGeometryError(ValueError):
    """A zone polygon cannot be used for geofencing."""


@dataclass
class CompiledZone:
    zone_id: str
    name: str
    zone_type: str
    severity: str
    polygon: Polygon
    prepared: object
    description: str = ""

    @property
    def area(self) -> float:
        return float(self.polygon.area)

    @property
    def centroid(self) -> tuple[float, float]:
        c = self.polygon.centroid
        return float(c.x), float(c.y)


def build_polygon(coords: Sequence[Sequence[float]], zone_id: str = "") -> Polygon:
    """Turn a [[x, y], ...] list into a validated Shapely polygon."""
    label = f"zone {zone_id}" if zone_id else "polygon"
    if not coords or len(coords) < 3:
        raise ZoneGeometryError(f"{label}: need at least 3 vertices, got {len(coords or [])}")
    try:
        ring = [(float(p[0]), float(p[1])) for p in coords]
    except (TypeError, ValueError, IndexError) as exc:
        raise ZoneGeometryError(f"{label}: vertices must be [x, y] pairs ({exc})") from exc

    # Collinear input is checked first: Shapely would otherwise report a
    # generic "invalid geometry" that does not say what is actually wrong.
    if _all_collinear(ring):
        raise ZoneGeometryError(f"{label}: polygon has zero area (collinear vertices)")

    poly = Polygon(ring)
    if poly.is_empty:
        raise ZoneGeometryError(f"{label}: polygon is empty")

    if not poly.is_valid:
        # A self-intersecting outline (the classic bow tie) is the usual cause.
        # buffer(0) is the standard Shapely repair. It can split the shape into
        # several pieces, in which case we keep the largest and warn, rather
        # than silently geofencing something the operator did not draw.
        repaired = poly.buffer(0)
        if repaired.is_empty:
            raise ZoneGeometryError(
                f"{label}: invalid polygon ({explain_validity(poly)}). "
                "Vertices must trace the outline without crossing over themselves."
            )
        if repaired.geom_type == "MultiPolygon":
            parts = sorted(repaired.geoms, key=lambda g: g.area, reverse=True)
            log.warning(
                "Zone %s self-intersects; repaired into %d parts, keeping the "
                "largest (%.1f m2). Redraw it to be sure of the coverage.",
                zone_id or "(unnamed)", len(parts), parts[0].area,
            )
            repaired = parts[0]
        elif repaired.geom_type != "Polygon":
            raise ZoneGeometryError(
                f"{label}: repairing the polygon produced a {repaired.geom_type}, "
                "which cannot be used as a zone."
            )
        else:
            log.warning("Zone %s had an invalid polygon; repaired with buffer(0)", zone_id)
        poly = repaired

    if poly.area <= 0:
        raise ZoneGeometryError(f"{label}: polygon has zero area")
    return poly


def _all_collinear(ring: list[tuple[float, float]]) -> bool:
    """True when every vertex lies on one straight line."""
    ax, ay = ring[0]
    spread = max(
        max(abs(x - ax) for x, _ in ring), max(abs(y - ay) for _, y in ring), 1.0
    )
    tol = spread * spread * 1e-9
    for i in range(1, len(ring) - 1):
        bx, by = ring[i]
        cx, cy = ring[i + 1]
        if abs((bx - ax) * (cy - ay) - (cx - ax) * (by - ay)) > tol:
            return False
    return True


@dataclass
class WorkerZoneState:
    """Which zones a worker is currently inside, and since when."""

    inside: set[str] = field(default_factory=set)
    entered_at: dict[str, float] = field(default_factory=dict)


@dataclass
class ZoneTransition:
    worker_id: int
    zone: CompiledZone
    event: str  # ENTER | EXIT
    dwell_seconds: float = 0.0


class GeofenceEngine:
    """
    Holds the compiled active zones and the per-worker inside/outside state.

    The state machine is what turns a per-frame point-in-polygon test into
    discrete ENTER / EXIT events, so the alert layer never sees one event per
    frame while a worker simply stands inside a zone.
    """

    def __init__(self) -> None:
        self._zones: dict[str, CompiledZone] = {}
        self._state: dict[int, WorkerZoneState] = {}

    # ------------------------------------------------------------ zone cache
    def load(self, zone_rows: Iterable) -> list[str]:
        """
        Rebuild the cache from DangerZone ORM rows (or any object exposing the
        same attributes). Returns the ids of zones that had to be skipped.
        """
        compiled: dict[str, CompiledZone] = {}
        skipped: list[str] = []
        for row in zone_rows:
            if not getattr(row, "active", True):
                continue
            try:
                poly = build_polygon(row.polygon, row.zone_id)
            except ZoneGeometryError as exc:
                log.error("Skipping zone %s: %s", row.zone_id, exc)
                skipped.append(row.zone_id)
                continue
            compiled[row.zone_id] = CompiledZone(
                zone_id=row.zone_id,
                name=row.name,
                zone_type=str(getattr(row.zone_type, "value", row.zone_type)),
                severity=str(getattr(row.severity, "value", row.severity)),
                polygon=poly,
                prepared=prep(poly),
                description=row.description or "",
            )
        self._zones = compiled

        # Drop remembered state for zones that no longer exist, otherwise a
        # deleted zone would keep a worker flagged as inside it forever.
        live = set(compiled)
        for st in self._state.values():
            stale = st.inside - live
            for z in stale:
                st.inside.discard(z)
                st.entered_at.pop(z, None)
        return skipped

    @property
    def zones(self) -> list[CompiledZone]:
        return list(self._zones.values())

    def get(self, zone_id: str) -> CompiledZone | None:
        return self._zones.get(zone_id)

    # ------------------------------------------------------------- queries
    def zones_containing(self, x: float, y: float) -> list[CompiledZone]:
        """All active zones whose polygon covers this point. Boundary counts."""
        pt = Point(x, y)
        return [z for z in self._zones.values() if z.prepared.covers(pt)]

    # ------------------------------------------------------- state machine
    def evaluate(
        self, worker_id: int, x: float, y: float, now: float
    ) -> tuple[list[CompiledZone], list[ZoneTransition]]:
        """
        Update the worker state for one observation.

        Returns (zones currently inside, transitions that just happened).
        """
        current = self.zones_containing(x, y)
        current_ids = {z.zone_id for z in current}

        state = self._state.setdefault(worker_id, WorkerZoneState())
        transitions: list[ZoneTransition] = []

        for zone in current:
            if zone.zone_id not in state.inside:
                state.inside.add(zone.zone_id)
                state.entered_at[zone.zone_id] = now
                transitions.append(ZoneTransition(worker_id, zone, "ENTER"))

        for zone_id in list(state.inside - current_ids):
            zone = self._zones.get(zone_id)
            entered = state.entered_at.pop(zone_id, now)
            state.inside.discard(zone_id)
            if zone is not None:
                transitions.append(
                    ZoneTransition(worker_id, zone, "EXIT", dwell_seconds=max(0.0, now - entered))
                )

        return current, transitions

    def dwell_seconds(self, worker_id: int, zone_id: str, now: float) -> float:
        st = self._state.get(worker_id)
        if not st or zone_id not in st.entered_at:
            return 0.0
        return max(0.0, now - st.entered_at[zone_id])

    def inside_zone_ids(self, worker_id: int) -> set[str]:
        st = self._state.get(worker_id)
        return set(st.inside) if st else set()

    def forget_worker(self, worker_id: int) -> list[str]:
        """Drop a worker that left the frame. Returns the zones they were in."""
        st = self._state.pop(worker_id, None)
        return list(st.inside) if st else []

    def reset(self) -> None:
        self._state.clear()


geofence_engine = GeofenceEngine()
