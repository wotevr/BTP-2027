"""Alert debouncing / lifecycle, and the worker attendance policy."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.database.models import AlertType, Severity, WorkerStatus
from app.schemas.detection import PPEStatus
from app.services.alert_service import AlertService
from app.services.worker_service import WorkerService

T0 = datetime(2026, 9, 22, 10, 0, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


@pytest.fixture
def alerts():
    return AlertService()


@pytest.fixture
def workers():
    return WorkerService()


# --------------------------------------------------------------------------- #
# PPE violations
# --------------------------------------------------------------------------- #
def test_ppe_violation_waits_for_the_dwell_threshold(alerts):
    """A single-frame flicker must not raise an alert."""
    assert alerts.evaluate_ppe(17, ["helmet"], (1.0, 2.0), at(0)) == []
    assert alerts.evaluate_ppe(17, ["helmet"], (1.0, 2.0), at(0.5)) == []

    events = alerts.evaluate_ppe(
        17, ["helmet"], (1.0, 2.0), at(settings.PPE_VIOLATION_MIN_SECONDS + 0.1)
    )
    assert len(events) == 1
    assert events[0].event_type is AlertType.PPE_MISSING
    assert events[0].ppe_item == "helmet"
    assert events[0].severity is Severity.HIGH


def test_flicker_resets_the_dwell_timer(alerts):
    alerts.evaluate_ppe(17, ["helmet"], (0, 0), at(0))
    alerts.evaluate_ppe(17, [], (0, 0), at(1))            # helmet reappears
    alerts.evaluate_ppe(17, ["helmet"], (0, 0), at(1.1))  # gone again, timer restarts
    assert alerts.evaluate_ppe(17, ["helmet"], (0, 0), at(2.0)) == []


def test_only_one_alert_while_the_violation_persists(alerts):
    alerts.evaluate_ppe(17, ["helmet"], (0, 0), at(0))
    raised = []
    for i in range(1, 200):          # 20 seconds at 10 fps
        raised += alerts.evaluate_ppe(17, ["helmet"], (0, 0), at(i * 0.1))
    assert len(raised) == 1, "debouncing should collapse 200 frames into 1 alert"
    assert len(alerts.active_alerts) == 1


def test_restoring_ppe_resolves_the_alert(alerts):
    alerts.evaluate_ppe(17, ["vest"], (0, 0), at(0))
    alerts.evaluate_ppe(17, ["vest"], (0, 0), at(3))
    assert len(alerts.active_alerts) == 1

    alerts.evaluate_ppe(17, [], (0, 0), at(4))
    assert alerts.active_alerts == []
    assert [r.event_id for r in alerts.drain_resolved()]


def test_severity_ranking_by_item(alerts):
    for item, expected in [("helmet", Severity.HIGH), ("vest", Severity.MEDIUM), ("mask", Severity.LOW)]:
        svc = AlertService()
        svc.evaluate_ppe(1, [item], (0, 0), at(0))
        events = svc.evaluate_ppe(1, [item], (0, 0), at(3))
        assert events[0].severity is expected


def test_unknown_ppe_is_never_a_violation():
    ppe = PPEStatus(helmet=None, vest=False, mask=True)
    assert ppe.missing_items(["helmet", "vest", "mask"]) == ["vest"]


# --------------------------------------------------------------------------- #
# Zone alerts
# --------------------------------------------------------------------------- #
def test_entering_raises_a_breach_and_a_log_entry(alerts):
    events = alerts.zone_entered(17, "machinery_01", "Heavy Machinery Zone", "HIGH", (14.2, 8.7), at(0))
    kinds = {e.event_type for e in events}
    assert kinds == {AlertType.WORKER_ENTERED_ZONE, AlertType.ZONE_BREACH}

    breach = next(e for e in events if e.event_type is AlertType.ZONE_BREACH)
    assert breach.severity is Severity.HIGH
    assert breach.location.x == 14.2
    assert "Heavy Machinery Zone" in breach.message


def test_entered_zone_is_momentary_and_breach_is_ongoing(alerts):
    events = alerts.zone_entered(17, "z", "Zone", "HIGH", (0, 0), at(0))
    enter = next(e for e in events if e.event_type is AlertType.WORKER_ENTERED_ZONE)
    breach = next(e for e in events if e.event_type is AlertType.ZONE_BREACH)
    assert enter.momentary is True
    assert breach.momentary is False
    # Only the ongoing condition sits in the active list.
    assert [a.alert_type for a in alerts.active_alerts] == [AlertType.ZONE_BREACH]


def test_breach_survives_as_long_as_it_is_re_confirmed(alerts):
    """
    The regression this guards: nothing re-confirms a zone breach frame by
    frame, so without confirm_zone_presence it would time out after
    ALERT_AUTO_RESOLVE_SECONDS while the worker was still standing there.
    """
    alerts.zone_entered(17, "z", "Zone", "HIGH", (0, 0), at(0))
    long_after = settings.ALERT_AUTO_RESOLVE_SECONDS * 3

    t = 0.0
    while t < long_after:
        t += 0.5
        alerts.confirm_zone_presence(17, ["z"], (0, 0), at(t))
        alerts.tick(at(t))

    assert len(alerts.active_alerts) == 1, "breach must stay open while inside"


def test_breach_times_out_when_evidence_stops(alerts):
    """Camera feed dies while the worker is inside: the alert must not hang."""
    alerts.zone_entered(17, "z", "Zone", "HIGH", (0, 0), at(0))
    alerts.tick(at(settings.ALERT_AUTO_RESOLVE_SECONDS + 1))
    assert alerts.active_alerts == []


def test_exiting_resolves_the_breach(alerts):
    alerts.zone_entered(17, "z", "Zone", "HIGH", (0, 0), at(0))
    events = alerts.zone_exited(17, "z", "Zone", dwell_seconds=12.0, world=(1, 1), now=at(12))
    assert [e.event_type for e in events] == [AlertType.WORKER_EXITED_ZONE]
    assert "12s" in events[0].message
    assert alerts.active_alerts == []


def test_every_resolution_is_queued_for_persistence(alerts):
    alerts.zone_entered(17, "z", "Zone", "HIGH", (0, 0), at(0))
    alerts.zone_exited(17, "z", "Zone", 5.0, (0, 0), at(5))
    resolved = alerts.drain_resolved()
    assert len(resolved) == 1
    assert alerts.drain_resolved() == [], "draining twice must not duplicate"


def test_cooldown_suppresses_a_rapid_retrigger(alerts):
    alerts.zone_entered(17, "z", "Zone", "HIGH", (0, 0), at(0))
    alerts.zone_exited(17, "z", "Zone", 1.0, (0, 0), at(1))
    # Straight back in, well inside the cooldown window.
    events = alerts.zone_entered(17, "z", "Zone", "HIGH", (0, 0), at(2))
    assert events == [], "re-entry inside the cooldown must not spam a new alert"


def test_worker_leaving_site_closes_everything(alerts):
    alerts.zone_entered(17, "z", "Zone", "HIGH", (0, 0), at(0))
    alerts.evaluate_ppe(17, ["helmet"], (0, 0), at(0))
    alerts.evaluate_ppe(17, ["helmet"], (0, 0), at(3))
    assert len(alerts.active_alerts) == 2

    alerts.forget_worker(17, at(10))
    assert alerts.active_alerts == []


def test_operator_acknowledgement(alerts):
    events = alerts.zone_entered(17, "z", "Zone", "HIGH", (0, 0), at(0))
    breach = next(e for e in events if e.event_type is AlertType.ZONE_BREACH)
    assert alerts.resolve_by_event_id(breach.event_id, at(1)) is not None
    assert alerts.active_alerts == []
    assert alerts.resolve_by_event_id("does-not-exist") is None


def test_alerts_are_isolated_per_worker(alerts):
    alerts.zone_entered(1, "z", "Zone", "HIGH", (0, 0), at(0))
    alerts.zone_entered(2, "z", "Zone", "HIGH", (0, 0), at(0))
    assert len(alerts.active_for_worker(1)) == 1
    assert len(alerts.active_for_worker(2)) == 1
    assert alerts.active_types_for_worker(1) == ["ZONE_BREACH"]


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
def observe(svc, worker_id, t, world=(1.0, 2.0)):
    return svc.observe(
        worker_id=worker_id,
        camera_id="camera_01",
        pixel=(100.0, 200.0),
        world=world,
        ppe=PPEStatus(),
        confidence=0.9,
        now=t,
    )


def test_first_sighting_creates_the_record(workers):
    state = observe(workers, 17, at(0))
    assert state.first_seen == at(0)
    assert state.total_time_seconds == 0.0


def test_continuous_presence_accumulates(workers):
    for i in range(11):
        observe(workers, 17, at(i))
    assert workers.get(17).total_time_seconds == pytest.approx(10.0)


def test_a_long_gap_is_not_counted_as_time_on_site(workers):
    """The lunch-break case: the worker was away, so the gap must not count."""
    observe(workers, 17, at(0))
    observe(workers, 17, at(5))
    observe(workers, 17, at(5 + settings.PRESENCE_GAP_SECONDS + 60))
    observe(workers, 17, at(5 + settings.PRESENCE_GAP_SECONDS + 61))
    assert workers.get(17).total_time_seconds == pytest.approx(6.0)


def test_a_short_gap_is_counted(workers):
    """A few dropped frames must not split the session."""
    observe(workers, 17, at(0))
    observe(workers, 17, at(settings.PRESENCE_GAP_SECONDS - 1))
    assert workers.get(17).total_time_seconds == pytest.approx(
        settings.PRESENCE_GAP_SECONDS - 1
    )


def test_status_flips_to_off_site_after_the_absence_threshold(workers):
    observe(workers, 17, datetime.now(timezone.utc))
    assert workers.get(17).status is WorkerStatus.ON_SITE

    stale = datetime.now(timezone.utc) - timedelta(seconds=settings.ABSENT_AFTER_SECONDS + 5)
    observe(workers, 18, stale)
    assert workers.get(18).status is WorkerStatus.OFF_SITE
    assert 18 in workers.gone_absent()
    assert 17 not in workers.gone_absent()


def test_telemetry_sampling_is_rate_limited(workers):
    observe(workers, 17, at(0))
    assert workers.should_sample_telemetry(17, at(0)) is True
    assert workers.should_sample_telemetry(17, at(0.01)) is False
    assert workers.should_sample_telemetry(
        17, at(settings.TELEMETRY_SAMPLE_INTERVAL + 0.01)
    ) is True


def test_sampling_is_per_worker_not_global(workers):
    observe(workers, 1, at(0))
    observe(workers, 2, at(0))
    assert workers.should_sample_telemetry(1, at(0)) is True
    assert workers.should_sample_telemetry(2, at(0)) is True


def test_marker_state_reflects_the_worst_alert(workers):
    state = observe(workers, 17, at(0))
    assert workers.to_live_worker(state, []).state == "SAFE"
    assert workers.to_live_worker(state, ["PPE_MISSING"]).state == "WARNING"
    assert workers.to_live_worker(state, ["PPE_MISSING", "ZONE_BREACH"]).state == "DANGER"


def test_world_position_is_omitted_without_calibration(workers):
    state = workers.observe(
        worker_id=17, camera_id="c", pixel=(10.0, 20.0), world=None,
        ppe=PPEStatus(), confidence=0.9, now=at(0),
    )
    live = workers.to_live_worker(state, [])
    assert live.world_position is None
    assert live.pixel_position.x == 10.0, "the worker is still tracked in pixel space"
