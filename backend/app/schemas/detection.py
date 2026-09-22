"""
Normalised detection schema: the contract between ANY vision pipeline and the
spatial safety layer.

This is the single place where the two systems meet. The upstream repository
(Construction_Safety_BTP) speaks YOLO/BoT-SORT; everything downstream of this
module speaks only these types.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# A PPE item is tri-state on purpose:
#   True  -> the detector saw the item on this worker
#   False -> the detector explicitly saw the "NO-<item>" class on this worker
#   None  -> nothing conclusive was observed this frame
PPEState = bool | None


class PPEStatus(BaseModel):
    """Per-worker PPE, derived by the vision adapter from class-level boxes."""

    model_config = ConfigDict(extra="allow")

    helmet: PPEState = None
    vest: PPEState = None
    mask: PPEState = None

    def missing_items(self, required: list[str]) -> list[str]:
        """Required items explicitly observed as absent. Unknown is not a violation."""
        out = []
        for item in required:
            if getattr(self, item, None) is False:
                out.append(item)
        return out

    def as_labels(self) -> dict[str, str]:
        """Human/UI form: OK / MISSING / UNKNOWN."""
        labels = {}
        for key, value in self.model_dump().items():
            labels[key] = "OK" if value is True else ("MISSING" if value is False else "UNKNOWN")
        return labels


class WorkerDetection(BaseModel):
    """One tracked person in one frame."""

    track_id: int = Field(..., ge=0, description="Persistent id from the upstream tracker")
    bbox: list[float] = Field(..., description="[x1, y1, x2, y2] in frame pixels")
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    ppe: PPEStatus = Field(default_factory=PPEStatus)

    @field_validator("bbox")
    @classmethod
    def _check_bbox(cls, v: list[float]) -> list[float]:
        if len(v) != 4:
            raise ValueError("bbox must have exactly 4 values: [x1, y1, x2, y2]")
        if any(not isinstance(c, (int, float)) for c in v):
            raise ValueError("bbox values must be numbers")
        x1, y1, x2, y2 = v
        if x2 <= x1 or y2 <= y1:
            raise ValueError(
                f"degenerate bbox: expected x1<x2 and y1<y2, got {v}"
            )
        return [float(c) for c in v]

    @property
    def foot_point(self) -> tuple[float, float]:
        """
        Ground-contact point = bottom-centre of the box.

        The centre of the box floats around chest height and would project to a
        point behind the worker on the ground plane; the bottom edge is where
        the worker actually touches the floor.
        """
        x1, _, x2, y2 = self.bbox
        return ((x1 + x2) / 2.0, y2)


class DetectionFrame(BaseModel):
    """The ingestion payload. One frame from one camera."""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    camera_id: str = "camera_01"
    frame_id: int = 0

    # Pixel space the bboxes live in. The upstream pipeline resizes to 640x480
    # before inference, so that is the default.
    frame_width: int = Field(640, gt=0)
    frame_height: int = Field(480, gt=0)

    source: Literal["live", "demo"] = "live"
    workers: list[WorkerDetection] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_tracks(self):
        seen = set()
        for w in self.workers:
            if w.track_id in seen:
                raise ValueError(f"duplicate track_id {w.track_id} in one frame")
            seen.add(w.track_id)
        return self


class IngestionResponse(BaseModel):
    success: bool
    processed_workers: int
    alerts_generated: int
    spatial_calibration: bool = Field(
        ..., description="False when no valid homography is loaded"
    )
    detail: str | None = None
