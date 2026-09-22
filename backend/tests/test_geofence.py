"""Geofencing: point-in-polygon, containment semantics, enter/exit hysteresis."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.services.geofence_engine import (
    GeofenceEngine,
    ZoneGeometryError,
    build_polygon,
)


@dataclass
class FakeZone:
    zone_id: str
    name: str
    zone_type: str
    severity: str
    polygon: list
    description: str = ""
    active: bool = True


SQUARE = [[10.0, 5.0], [20.0, 5.0], [20.0, 15.0], [10.0, 15.0]]


def machinery(**kw):
    base = dict(
        zone_id="machinery_01",
        name="Heavy Machinery Zone",
        zone_type="HEAVY_MACHINERY",
        severity="HIGH",
        polygon=SQUARE,
    )
    base.update(kw)
    return FakeZone(**base)


@pytest.fixture
def engine():
    e = GeofenceEngine()
    e.load([machinery()])
    return e


# --------------------------------------------------------------------------- #
# Polygon building
# --------------------------------------------------------------------------- #
def test_builds_a_valid_polygon():
    poly = build_polygon(SQUARE)
    assert poly.area == pytest.approx(100.0)


def test_rejects_fewer_than_three_vertices():
    with pytest.raises(ZoneGeometryError, match="at least 3"):
        build_polygon([[0, 0], [1, 1]])


def test_rejects_zero_area_polygon():
    with pytest.raises(ZoneGeometryError, match="zero area"):
        build_polygon([[0, 0], [5, 5], [10, 10]])


def test_repairs_a_self_intersecting_bow_tie():
    """buffer(0) is the standard Shapely repair; the zone stays usable."""
    poly = build_polygon([[0, 0], [10, 10], [10, 0], [0, 10]])
    assert poly.is_valid
    assert poly.area > 0


def test_rejects_non_numeric_vertices():
    with pytest.raises(ZoneGeometryError):
        build_polygon([["a", "b"], [1, 0], [1, 1]])


# --------------------------------------------------------------------------- #
# Membership
# --------------------------------------------------------------------------- #
def test_point_inside_is_detected(engine):
    assert [z.zone_id for z in engine.zones_containing(15.0, 10.0)] == ["machinery_01"]


def test_point_outside_is_not(engine):
    assert engine.zones_containing(5.0, 5.0) == []
    assert engine.zones_containing(25.0, 10.0) == []


@pytest.mark.parametrize("point", [(10.0, 10.0), (20.0, 10.0), (15.0, 5.0), (10.0, 5.0)])
def test_boundary_counts_as_inside(engine, point):
    """
    `covers()` not `contains()`: a worker standing exactly on the painted edge
    of an excavation is in danger, so the boundary must count.
    """
    assert engine.zones_containing(*point), f"{point} on the boundary should be inside"


def test_inactive_zones_are_not_loaded():
    e = GeofenceEngine()
    e.load([machinery(active=False)])
    assert e.zones == []
    assert e.zones_containing(15.0, 10.0) == []


def test_invalid_zone_is_skipped_not_fatal():
    e = GeofenceEngine()
    skipped = e.load([machinery(), machinery(zone_id="bad", polygon=[[0, 0], [1, 1]])])
    assert skipped == ["bad"]
    assert [z.zone_id for z in e.zones] == ["machinery_01"]


def test_overlapping_zones_both_report():
    e = GeofenceEngine()
    e.load([machinery(), machinery(zone_id="second", name="Second", polygon=[[14, 9], [25, 9], [25, 18], [14, 18]])])
    assert len(e.zones_containing(15.0, 10.0)) == 2


# --------------------------------------------------------------------------- #
# Enter / exit hysteresis
# --------------------------------------------------------------------------- #
def test_entering_emits_exactly_one_enter(engine):
    _, t1 = engine.evaluate(17, 15.0, 10.0, now=0.0)
    assert [(t.event, t.zone.zone_id) for t in t1] == [("ENTER", "machinery_01")]

    # Standing still inside must produce nothing further - this is what stops
    # the alert engine from seeing one event per frame.
    for i in range(1, 50):
        _, t = engine.evaluate(17, 15.0, 10.0, now=float(i) * 0.1)
        assert t == []


def test_leaving_emits_exit_with_dwell_time(engine):
    engine.evaluate(17, 15.0, 10.0, now=0.0)
    _, transitions = engine.evaluate(17, 30.0, 30.0, now=12.5)
    assert len(transitions) == 1
    assert transitions[0].event == "EXIT"
    assert transitions[0].dwell_seconds == pytest.approx(12.5)


def test_re_entering_emits_a_second_enter(engine):
    engine.evaluate(17, 15.0, 10.0, now=0.0)
    engine.evaluate(17, 30.0, 30.0, now=5.0)
    _, transitions = engine.evaluate(17, 15.0, 10.0, now=9.0)
    assert [t.event for t in transitions] == ["ENTER"]


def test_workers_have_independent_state(engine):
    engine.evaluate(1, 15.0, 10.0, now=0.0)
    _, t = engine.evaluate(2, 15.0, 10.0, now=0.0)
    assert [x.event for x in t] == ["ENTER"], "worker 2 entering is its own event"
    assert engine.inside_zone_ids(1) == {"machinery_01"}
    assert engine.inside_zone_ids(2) == {"machinery_01"}


def test_dwell_seconds_reported(engine):
    engine.evaluate(17, 15.0, 10.0, now=100.0)
    assert engine.dwell_seconds(17, "machinery_01", now=107.0) == pytest.approx(7.0)


def test_forget_worker_clears_membership(engine):
    engine.evaluate(17, 15.0, 10.0, now=0.0)
    assert engine.forget_worker(17) == ["machinery_01"]
    assert engine.inside_zone_ids(17) == set()


def test_deleting_a_zone_clears_stale_membership(engine):
    """A deleted zone must not leave a worker flagged inside it forever."""
    engine.evaluate(17, 15.0, 10.0, now=0.0)
    assert engine.inside_zone_ids(17) == {"machinery_01"}
    engine.load([])
    assert engine.inside_zone_ids(17) == set()
