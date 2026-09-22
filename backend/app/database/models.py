"""
SQLAlchemy models for the spatial safety layer.

Scope note: this database deliberately stores only spatial and safety state.
Identity, authentication and HR profiles belong to the upstream vision
repository. Here a worker is identified by the persistent track id that the
upstream BoT-SORT tracker assigns.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """
    A timestamp that is always timezone-aware UTC in Python.

    SQLite has no native timestamp type: it stores the string and hands back a
    NAIVE datetime, even for DateTime(timezone=True). Mixing those naive values
    with the timezone-aware ones held in memory raises
    "can't compare offset-naive and offset-aware datetimes" the moment anything
    sorts or subtracts them. Normalising at the type boundary fixes it once,
    everywhere, instead of sprinkling defensive conversions through the routes.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- #
# Enumerations (stored as plain strings so the DB stays readable)
# --------------------------------------------------------------------------- #
class AlertType(str, enum.Enum):
    PPE_MISSING = "PPE_MISSING"
    ZONE_BREACH = "ZONE_BREACH"
    WORKER_ENTERED_ZONE = "WORKER_ENTERED_ZONE"
    WORKER_EXITED_ZONE = "WORKER_EXITED_ZONE"
    SYSTEM_WARNING = "SYSTEM_WARNING"


class Severity(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class AlertStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"


class ZoneType(str, enum.Enum):
    HEAVY_MACHINERY = "HEAVY_MACHINERY"
    OPEN_EDGE = "OPEN_EDGE"
    EXCAVATION = "EXCAVATION"
    RESTRICTED_AREA = "RESTRICTED_AREA"
    OTHER = "OTHER"


class WorkerStatus(str, enum.Enum):
    ON_SITE = "ON_SITE"
    OFF_SITE = "OFF_SITE"


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #
class WorkerAttendance(Base):
    """
    One row per tracked worker, keyed by the upstream persistent track id.

    total_time_seconds accumulates only the gaps between two sightings that are
    closer together than PRESENCE_GAP_SECONDS. See services/worker_service.py
    for the full presence policy.
    """

    __tablename__ = "worker_attendance"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_id: Mapped[int] = mapped_column(
        Integer, unique=True, index=True, nullable=False
    )
    camera_id: Mapped[str] = mapped_column(String(64), default="camera_01")

    first_seen: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, index=True
    )
    total_time_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    current_status: Mapped[str] = mapped_column(
        String(16), default=WorkerStatus.ON_SITE.value, index=True
    )

    # Denormalised last-known position so the workers list needs no join.
    last_world_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_world_y: Mapped[float | None] = mapped_column(Float, nullable=True)
    incident_count: Mapped[int] = mapped_column(Integer, default=0)

    telemetry: Mapped[list["LocationTelemetry"]] = relationship(
        back_populates="worker", cascade="all, delete-orphan", passive_deletes=True
    )
    alerts: Mapped[list["SafetyAlert"]] = relationship(
        back_populates="worker", cascade="all, delete-orphan", passive_deletes=True
    )


class LocationTelemetry(Base):
    """
    Sampled worker position.

    Both pixel and world coordinates are kept so that a later recalibration can
    be re-applied to historical data.
    """

    __tablename__ = "location_telemetry"
    __table_args__ = (Index("ix_telemetry_worker_time", "worker_id", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_id: Mapped[int] = mapped_column(
        ForeignKey("worker_attendance.worker_id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    timestamp: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, index=True
    )

    pixel_x: Mapped[float] = mapped_column(Float)
    pixel_y: Mapped[float] = mapped_column(Float)
    # Null when no valid homography was loaded at the time of the sighting.
    world_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    world_y: Mapped[float | None] = mapped_column(Float, nullable=True)

    zone_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    worker: Mapped["WorkerAttendance"] = relationship(back_populates="telemetry")


class SafetyAlert(Base):
    __tablename__ = "safety_alerts"
    __table_args__ = (
        Index("ix_alert_status_time", "status", "timestamp"),
        Index("ix_alert_dedupe", "worker_id", "alert_type", "zone_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    worker_id: Mapped[int] = mapped_column(
        ForeignKey("worker_attendance.worker_id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    alert_type: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(16), index=True)
    message: Mapped[str] = mapped_column(Text)

    zone_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ppe_item: Mapped[str | None] = mapped_column(String(32), nullable=True)

    world_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    world_y: Mapped[float | None] = mapped_column(Float, nullable=True)

    timestamp: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default=AlertStatus.ACTIVE.value, index=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )

    worker: Mapped["WorkerAttendance"] = relationship(back_populates="alerts")


class DangerZone(Base):
    """A polygon in site world coordinates (metres). Not pixels, not GPS."""

    __tablename__ = "danger_zones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zone_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    zone_type: Mapped[str] = mapped_column(String(32), default=ZoneType.OTHER.value)
    severity: Mapped[str] = mapped_column(String(16), default=Severity.MEDIUM.value)

    # [[x, y], [x, y], ...] in metres, at least 3 vertices.
    polygon: Mapped[list] = mapped_column(JSON)
    description: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )
