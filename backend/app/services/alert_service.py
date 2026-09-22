"""
The safety event engine.

This is where a per-frame boolean ("worker 17 is inside the machinery polygon")
becomes a discrete, human-meaningful incident.

THE DEBOUNCING PROBLEM
----------------------
The pipeline evaluates every frame. At 10 FPS a worker who stands in a danger
zone for 10 seconds produces 100 positive tests. Emitting an alert per test
would give a useless dashboard and a useless database.

Three mechanisms keep the event stream sane:

1. HYSTERESIS (zones). The geofence engine only reports ENTER and EXIT
   transitions, so a worker standing still generates nothing after the entry.

2. DWELL THRESHOLD (PPE). A PPE item must be reported missing continuously for
   PPE_VIOLATION_MIN_SECONDS before an alert is raised. Detectors flicker
   between `Hardhat` and `NO-Hardhat` on single frames; this rides over that.

3. COOLDOWN + LIFECYCLE. While a condition holds, exactly one ACTIVE alert
   exists. Re-triggering the same (worker, type, subject) is suppressed for
   ALERT_COOLDOWN_SECONDS. When the condition stops being re-confirmed for
   ALERT_AUTO_RESOLVE_SECONDS the alert transitions to RESOLVED.

The alert object is the state; the frame-by-frame evidence is not stored.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from app.core.config import settings
from app.database.models import AlertType, Severity
from app.schemas.alert import Location, SafetyEvent

log = logging.getLogger(__name__)

# Which PPE item maps to which severity. Head protection outranks the rest.
PPE_SEVERITY: dict[str, Severity] = {
    "helmet": Severity.HIGH,
    "vest": Severity.MEDIUM,
    "mask": Severity.LOW,
}

PPE_LABEL: dict[str, str] = {
    "helmet": "Hardhat",
    "vest": "Safety Vest",
    "mask": "Mask",
}

# Key that identifies one ongoing condition.
AlertKey = tuple[int, str, str]


@dataclass
class ActiveAlert:
    event_id: str
    worker_id: int
    alert_type: AlertType
    severity: Severity
    message: str
    subject: str                    # zone_id for zones, ppe item for PPE
    raised_at: datetime
    last_confirmed_at: datetime
    world_x: float | None = None
    world_y: float | None = None
    momentary: bool = False

    def to_event(self) -> SafetyEvent:
        return SafetyEvent(
            event_id=self.event_id,
            worker_id=self.worker_id,
            event_type=self.alert_type,
            severity=self.severity,
            zone_id=self.subject if self.alert_type != AlertType.PPE_MISSING else None,
            ppe_item=self.subject if self.alert_type == AlertType.PPE_MISSING else None,
            message=self.message,
            timestamp=self.raised_at,
            location=Location(x=self.world_x, y=self.world_y),
            momentary=self.momentary,
        )


@dataclass
class ResolvedAlert:
    event_id: str
    worker_id: int
    alert_type: AlertType
    resolved_at: datetime


@dataclass
class _PendingPPE:
    """A PPE item seen missing but not yet missing for long enough."""

    since: datetime
    last_seen_missing: datetime


@dataclass
class AlertEngineStats:
    raised: int = 0
    suppressed: int = 0
    resolved: int = 0


class AlertService:
    def __init__(self) -> None:
        self._active: dict[AlertKey, ActiveAlert] = {}
        # Last time each key produced an alert, for the cooldown.
        self._last_raised: dict[AlertKey, datetime] = {}
        self._pending_ppe: dict[tuple[int, str], _PendingPPE] = {}
        self._resolved_buffer: list[ResolvedAlert] = []
        self.stats = AlertEngineStats()

    # ------------------------------------------------------------- queries
    @property
    def active_alerts(self) -> list[ActiveAlert]:
        return list(self._active.values())

    def active_for_worker(self, worker_id: int) -> list[ActiveAlert]:
        return [a for a in self._active.values() if a.worker_id == worker_id]

    def active_types_for_worker(self, worker_id: int) -> list[str]:
        return [a.alert_type.value for a in self._active.values() if a.worker_id == worker_id]

    def worst_severity_for_worker(self, worker_id: int) -> Severity | None:
        order = [Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
        sevs = [a.severity for a in self.active_for_worker(worker_id)]
        return max(sevs, key=order.index) if sevs else None

    # --------------------------------------------------------------- core
    def _raise(
        self,
        key: AlertKey,
        worker_id: int,
        alert_type: AlertType,
        severity: Severity,
        message: str,
        subject: str,
        now: datetime,
        world: tuple[float | None, float | None],
        momentary: bool = False,
    ) -> SafetyEvent | None:
        existing = self._active.get(key)
        if existing is not None:
            # Same condition still holding - refresh it, emit nothing.
            existing.last_confirmed_at = now
            existing.world_x, existing.world_y = world
            self.stats.suppressed += 1
            return None

        last = self._last_raised.get(key)
        if last is not None and (now - last).total_seconds() < settings.ALERT_COOLDOWN_SECONDS:
            self.stats.suppressed += 1
            return None

        alert = ActiveAlert(
            event_id=uuid.uuid4().hex[:16],
            worker_id=worker_id,
            alert_type=alert_type,
            severity=severity,
            message=message,
            subject=subject,
            raised_at=now,
            last_confirmed_at=now,
            world_x=world[0],
            world_y=world[1],
            momentary=momentary,
        )
        # A momentary fact is logged, not held open, so it never appears in the
        # active list and never needs resolving.
        if not momentary:
            self._active[key] = alert
        self._last_raised[key] = now
        self.stats.raised += 1
        log.info("ALERT %s | worker %s | %s", alert_type.value, worker_id, message)
        return alert.to_event()

    # ------------------------------------------------------------- zones
    def zone_entered(
        self,
        worker_id: int,
        zone_id: str,
        zone_name: str,
        zone_severity: str,
        world: tuple[float | None, float | None],
        now: datetime | None = None,
    ) -> list[SafetyEvent]:
        """
        A worker crossed into a danger zone.

        Two events are produced on purpose:
          * WORKER_ENTERED_ZONE - the movement fact, always LOW, momentary, for
            the audit trail and the movement timeline.
          * ZONE_BREACH - the safety incident, carrying the severity of the
            zone. This is the ongoing condition the control room reacts to, and
            it stays ACTIVE until the worker leaves.
        """
        now = now or datetime.now(timezone.utc)
        try:
            severity = Severity(zone_severity)
        except ValueError:
            severity = Severity.MEDIUM

        events: list[SafetyEvent] = []
        enter = self._raise(
            (worker_id, AlertType.WORKER_ENTERED_ZONE.value, zone_id),
            worker_id, AlertType.WORKER_ENTERED_ZONE, Severity.LOW,
            f"Worker #{worker_id} entered {zone_name}", zone_id, now, world,
            momentary=True,
        )
        if enter:
            events.append(enter)

        breach = self._raise(
            (worker_id, AlertType.ZONE_BREACH.value, zone_id),
            worker_id, AlertType.ZONE_BREACH, severity,
            f"Worker #{worker_id} is inside {zone_name}", zone_id, now, world,
        )
        if breach:
            events.append(breach)
        return events

    def confirm_zone_presence(
        self,
        worker_id: int,
        zone_ids: list[str],
        world: tuple[float | None, float | None],
        now: datetime | None = None,
    ) -> None:
        """
        Called every frame for the zones a worker is still inside.

        Without this the breach would be auto-resolved by `tick()` roughly
        ALERT_AUTO_RESOLVE_SECONDS after entry, even though the worker never
        left - because nothing else re-confirms an ongoing zone condition.
        Re-confirming here is also what makes the timeout meaningful: it only
        fires when the evidence genuinely stops arriving.
        """
        now = now or datetime.now(timezone.utc)
        for zone_id in zone_ids:
            alert = self._active.get((worker_id, AlertType.ZONE_BREACH.value, zone_id))
            if alert is not None:
                alert.last_confirmed_at = now
                alert.world_x, alert.world_y = world

    def zone_exited(
        self,
        worker_id: int,
        zone_id: str,
        zone_name: str,
        dwell_seconds: float,
        world: tuple[float | None, float | None],
        now: datetime | None = None,
    ) -> list[SafetyEvent]:
        """
        Worker left the zone.

        Returns the exit event plus the breach that this exit closed, so the
        caller can mark the stored alert RESOLVED.
        """
        now = now or datetime.now(timezone.utc)
        events: list[SafetyEvent] = []

        closed = self._resolve((worker_id, AlertType.ZONE_BREACH.value, zone_id), now)

        exit_event = self._raise(
            (worker_id, AlertType.WORKER_EXITED_ZONE.value, zone_id),
            worker_id, AlertType.WORKER_EXITED_ZONE, Severity.LOW,
            f"Worker #{worker_id} left {zone_name} after {dwell_seconds:.0f}s",
            zone_id, now, world,
            momentary=True,
        )
        if exit_event:
            events.append(exit_event)

        del closed  # already queued for persistence by _resolve
        return events

    # --------------------------------------------------------------- PPE
    def evaluate_ppe(
        self,
        worker_id: int,
        missing_items: list[str],
        world: tuple[float | None, float | None],
        now: datetime | None = None,
    ) -> list[SafetyEvent]:
        """
        Apply the dwell threshold to the PPE items reported missing this frame.

        `missing_items` must contain only items the detector explicitly reported
        as absent. Items it simply did not observe are unknown, and unknown is
        never a violation.
        """
        now = now or datetime.now(timezone.utc)
        events: list[SafetyEvent] = []
        missing = set(missing_items)

        for item in missing:
            pending = self._pending_ppe.get((worker_id, item))
            if pending is None:
                self._pending_ppe[(worker_id, item)] = _PendingPPE(since=now, last_seen_missing=now)
                continue
            pending.last_seen_missing = now
            if (now - pending.since).total_seconds() < settings.PPE_VIOLATION_MIN_SECONDS:
                continue

            label = PPE_LABEL.get(item, item.title())
            event = self._raise(
                (worker_id, AlertType.PPE_MISSING.value, item),
                worker_id,
                AlertType.PPE_MISSING,
                PPE_SEVERITY.get(item, Severity.MEDIUM),
                f"Worker #{worker_id} is not wearing a {label}",
                item, now, world,
            )
            if event:
                events.append(event)

        # Items no longer missing: clear the pending timer and close the alert.
        for (wid, item) in [k for k in self._pending_ppe if k[0] == worker_id]:
            if item not in missing:
                self._pending_ppe.pop((wid, item), None)
                self._resolve((worker_id, AlertType.PPE_MISSING.value, item), now)

        return events

    # ---------------------------------------------------------- lifecycle
    def _resolve(self, key: AlertKey, now: datetime) -> ResolvedAlert | None:
        alert = self._active.pop(key, None)
        if alert is None:
            return None
        self.stats.resolved += 1
        log.info("RESOLVED %s | worker %s", alert.alert_type.value, alert.worker_id)
        resolved = ResolvedAlert(alert.event_id, alert.worker_id, alert.alert_type, now)
        # Buffered so that EVERY resolution reaches the database, no matter
        # which code path closed it (exit, PPE restored, timeout, worker gone,
        # operator acknowledgement).
        self._resolved_buffer.append(resolved)
        return resolved

    def drain_resolved(self) -> list[ResolvedAlert]:
        """Take the resolutions accumulated since the last call."""
        out = self._resolved_buffer
        self._resolved_buffer = []
        return out

    def tick(self, now: datetime | None = None) -> list[ResolvedAlert]:
        """
        Close alerts whose condition stopped being re-confirmed.

        This is what handles a worker who simply walks out of the camera view
        while still inside a zone: no EXIT transition is ever seen, so the
        alert has to time out instead.
        """
        now = now or datetime.now(timezone.utc)
        cutoff = settings.ALERT_AUTO_RESOLVE_SECONDS
        stale = [
            key
            for key, alert in self._active.items()
            if (now - alert.last_confirmed_at).total_seconds() > cutoff
        ]
        resolved = []
        for key in stale:
            r = self._resolve(key, now)
            if r:
                resolved.append(r)

        # Drop pending PPE timers that were never confirmed again.
        for key in [
            k for k, v in self._pending_ppe.items()
            if (now - v.last_seen_missing).total_seconds() > cutoff
        ]:
            self._pending_ppe.pop(key, None)
        return resolved

    def resolve_by_event_id(
        self, event_id: str, now: datetime | None = None
    ) -> ResolvedAlert | None:
        """
        Operator acknowledgement from the dashboard.

        The cooldown timestamp is left in place on purpose: acknowledging an
        alert should not immediately re-raise it on the very next frame while
        the condition is still true.
        """
        now = now or datetime.now(timezone.utc)
        for key, alert in list(self._active.items()):
            if alert.event_id == event_id:
                return self._resolve(key, now)
        return None

    def forget_worker(self, worker_id: int, now: datetime | None = None) -> list[ResolvedAlert]:
        """Worker left the site: close everything still open for them."""
        now = now or datetime.now(timezone.utc)
        resolved = []
        for key in [k for k in self._active if k[0] == worker_id]:
            r = self._resolve(key, now)
            if r:
                resolved.append(r)
        for key in [k for k in self._pending_ppe if k[0] == worker_id]:
            self._pending_ppe.pop(key, None)
        return resolved

    def reset(self) -> None:
        self._active.clear()
        self._last_raised.clear()
        self._pending_ppe.clear()
        self._resolved_buffer.clear()
        self.stats = AlertEngineStats()


alert_service = AlertService()
