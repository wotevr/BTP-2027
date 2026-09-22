"""
Pixel to ground-plane transformation.

A single fixed camera looking at a flat construction site sees the ground plane
through a projective transform. Four or more correspondences between image
points and measured ground points determine a 3x3 homography H such that

    [u', v', w']^T  =  H . [X, Y, 1]^T          (world -> image)
    (u, v)          =  (u'/w', v'/w')

We invert H to go from a pixel on the ground to metres on the site.

IMPORTANT: the output is a LOCAL SITE coordinate system in metres.

    X = metres along the site width
    Y = metres along the site depth

It is not GPS, and it is only valid for points that actually lie on the ground
plane. That is exactly why the foot point (bottom-centre of the bounding box)
is used rather than the box centre.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import cv2
import numpy as np

MIN_POINTS = 4


class HomographyError(ValueError):
    """Raised for any calibration problem the caller should see and fix."""


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def _as_point_array(points: Sequence[Sequence[float]], label: str) -> np.ndarray:
    if points is None:
        raise HomographyError(f"{label} is missing")
    if len(points) < MIN_POINTS:
        raise HomographyError(
            f"{label} needs at least {MIN_POINTS} points, got {len(points)}"
        )
    arr: list[list[float]] = []
    for i, p in enumerate(points):
        if not isinstance(p, (list, tuple, np.ndarray)) or len(p) != 2:
            raise HomographyError(f"{label}[{i}] must be a pair [a, b], got {p!r}")
        try:
            a, b = float(p[0]), float(p[1])
        except (TypeError, ValueError):
            raise HomographyError(
                f"{label}[{i}] has non-numeric values: {p!r}"
            ) from None
        if not (math.isfinite(a) and math.isfinite(b)):
            raise HomographyError(f"{label}[{i}] contains NaN or infinity")
        arr.append([a, b])
    return np.asarray(arr, dtype=np.float64)


def _count_collinear_triples(pts: np.ndarray, tol: float) -> int:
    """Number of point triples that are (near) collinear."""
    n = len(pts)
    bad = 0
    for i in range(n):
        for j in range(i + 1, n):
            for k in range(j + 1, n):
                ax, ay = pts[i]
                bx, by = pts[j]
                cx, cy = pts[k]
                area2 = abs((bx - ax) * (cy - ay) - (cx - ax) * (by - ay))
                if area2 < tol:
                    bad += 1
    return bad


def _assert_non_degenerate(pts: np.ndarray, label: str) -> None:
    """
    A homography needs 4 points in general position. If any 3 of exactly 4 are
    collinear the system is rank-deficient and cv2 returns garbage (or None).
    """
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            if np.allclose(pts[i], pts[j], atol=1e-9):
                raise HomographyError(
                    f"{label}[{i}] and {label}[{j}] are the same point - "
                    "each correspondence must be a distinct location"
                )

    spread = float(np.max(pts, axis=0).max() - np.min(pts, axis=0).min())
    tol = max(spread, 1.0) * 1e-6
    triples = _count_collinear_triples(pts, tol)

    if len(pts) == MIN_POINTS and triples > 0:
        raise HomographyError(
            f"{label}: three or more points are collinear. With exactly 4 points "
            "they must form a proper quadrilateral - for example the 4 corners "
            "of a rectangle marked on the ground - not a straight line."
        )
    if triples == math.comb(len(pts), 3):
        raise HomographyError(f"{label}: all points are collinear")


# --------------------------------------------------------------------------- #
# The transform
# --------------------------------------------------------------------------- #
class HomographyTransform:
    """World/pixel transform for one fixed camera."""

    def __init__(
        self,
        matrix: Sequence[Sequence[float]],
        image_points: Sequence[Sequence[float]] | None = None,
        world_points: Sequence[Sequence[float]] | None = None,
        image_size: tuple[int, int] | None = None,
        created_at: str | None = None,
    ) -> None:
        H = np.asarray(matrix, dtype=np.float64)
        if H.shape != (3, 3):
            raise HomographyError(f"homography must be 3x3, got shape {H.shape}")
        if not np.all(np.isfinite(H)):
            raise HomographyError("homography contains NaN or infinity")
        det = float(np.linalg.det(H))
        if abs(det) < 1e-12:
            raise HomographyError(
                f"homography is singular (determinant {det:.3e}); the calibration "
                "points are almost certainly degenerate"
            )

        # Normalise so H[2][2] == 1 where possible: makes stored files comparable.
        if abs(H[2, 2]) > 1e-12:
            H = H / H[2, 2]

        self.matrix = H                       # world -> pixel
        self.inv_matrix = np.linalg.inv(H)    # pixel -> world
        self.image_points = [[float(a), float(b)] for a, b in (image_points or [])]
        self.world_points = [[float(a), float(b)] for a, b in (world_points or [])]
        # Pixel size the calibration was measured in. Detections arriving at a
        # different resolution are rescaled to this before being transformed.
        self.image_size = tuple(image_size) if image_size else None
        self.created_at = created_at or datetime.now(timezone.utc).isoformat()

    # -------------------------------------------------------------- building
    @classmethod
    def from_points(
        cls,
        image_points: Sequence[Sequence[float]],
        world_points: Sequence[Sequence[float]],
        image_size: tuple[int, int] | None = None,
    ) -> "HomographyTransform":
        img = _as_point_array(image_points, "image_points")
        wld = _as_point_array(world_points, "world_points")
        if len(img) != len(wld):
            raise HomographyError(
                f"image_points ({len(img)}) and world_points ({len(wld)}) must "
                "have the same length - they are correspondences"
            )
        _assert_non_degenerate(img, "image_points")
        _assert_non_degenerate(wld, "world_points")

        # World -> image. RANSAC only has something to vote on above 4 points.
        method = cv2.RANSAC if len(img) > MIN_POINTS else 0
        H, _mask = cv2.findHomography(wld, img, method, 3.0)
        if H is None:
            raise HomographyError(
                "cv2.findHomography failed. The correspondences are inconsistent "
                "or degenerate - re-check that point i in the image is the same "
                "physical location as point i on the ground."
            )
        return cls(
            H,
            image_points=img.tolist(),
            world_points=wld.tolist(),
            image_size=image_size,
        )

    # ------------------------------------------------------------ transforms
    def pixel_to_world(self, u: float, v: float) -> tuple[float, float]:
        """Ground pixel -> site metres."""
        pts = np.array([[[float(u), float(v)]]], dtype=np.float64)
        out = cv2.perspectiveTransform(pts, self.inv_matrix)
        x, y = float(out[0][0][0]), float(out[0][0][1])
        if not (math.isfinite(x) and math.isfinite(y)):
            raise HomographyError(
                f"pixel ({u}, {v}) maps to infinity - it lies on or beyond the "
                "camera horizon and has no ground-plane position"
            )
        return x, y

    def world_to_pixel(self, x: float, y: float) -> tuple[float, float]:
        """Site metres -> ground pixel."""
        pts = np.array([[[float(x), float(y)]]], dtype=np.float64)
        out = cv2.perspectiveTransform(pts, self.matrix)
        u, v = float(out[0][0][0]), float(out[0][0][1])
        if not (math.isfinite(u) and math.isfinite(v)):
            raise HomographyError(f"world ({x}, {y}) does not project into the image")
        return u, v

    def pixels_to_world(
        self, points: Sequence[Sequence[float]]
    ) -> list[tuple[float, float]]:
        """Batch version - one cv2 call for many points."""
        if len(points) == 0:
            return []
        pts = np.asarray(points, dtype=np.float64).reshape(-1, 1, 2)
        out = cv2.perspectiveTransform(pts, self.inv_matrix).reshape(-1, 2)
        return [(float(a), float(b)) for a, b in out]

    def scale_pixel(
        self, u: float, v: float, width: int, height: int
    ) -> tuple[float, float]:
        """
        Rescale a pixel measured in a width x height frame into the frame size
        the calibration was made in. This lets the upstream pipeline change its
        inference resolution without invalidating the calibration.
        """
        if not self.image_size:
            return u, v
        cw, ch = self.image_size
        if not width or not height or (width == cw and height == ch):
            return u, v
        return u * (cw / width), v * (ch / height)

    # --------------------------------------------------------------- quality
    def reprojection_error(self) -> float | None:
        """Mean pixel distance between measured and reprojected image points."""
        if not self.image_points or not self.world_points:
            return None
        try:
            proj = [self.world_to_pixel(x, y) for x, y in self.world_points]
        except HomographyError:
            return None
        errs = [math.dist(p, q) for p, q in zip(proj, self.image_points)]
        return float(sum(errs) / len(errs)) if errs else None

    # ------------------------------------------------------------ (de)serial
    def to_dict(self) -> dict[str, Any]:
        return {
            "image_points": self.image_points,
            "world_points": self.world_points,
            "homography": self.matrix.tolist(),
            "image_size": list(self.image_size) if self.image_size else None,
            "created_at": self.created_at,
            "reprojection_error_px": self.reprojection_error(),
            "note": (
                "world coordinates are LOCAL SITE metres "
                "(X = width, Y = depth), not GPS"
            ),
        }

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path) -> "HomographyTransform":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"calibration file not found: {p}")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise HomographyError(f"{p} is not valid JSON: {exc}") from exc

        matrix = data.get("homography")
        img = data.get("image_points")
        wld = data.get("world_points")
        size = data.get("image_size")

        if matrix is None:
            # Recompute from the correspondences if only those were stored.
            if img and wld:
                return cls.from_points(img, wld, tuple(size) if size else None)
            raise HomographyError(
                f"{p} has neither a 'homography' matrix nor image/world point pairs"
            )
        return cls(
            matrix,
            image_points=img,
            world_points=wld,
            image_size=tuple(size) if size else None,
            created_at=data.get("created_at"),
        )


# --------------------------------------------------------------------------- #
# Process-wide holder. Degrades gracefully when no calibration exists.
# --------------------------------------------------------------------------- #
class CalibrationState:
    def __init__(self) -> None:
        self.transform: HomographyTransform | None = None
        self.error: str | None = None
        self.path: Path | None = None

    @property
    def available(self) -> bool:
        return self.transform is not None

    def load(self, path: str | Path) -> None:
        self.path = Path(path)
        try:
            self.transform = HomographyTransform.load(path)
            self.error = None
        except FileNotFoundError:
            self.transform = None
            self.error = (
                f"Spatial calibration unavailable: no homography file at {path}. "
                "Run  python scripts/calibrate_homography.py  to create one. "
                "Workers are still tracked, but only in pixel space."
            )
        except HomographyError as exc:
            self.transform = None
            self.error = f"Spatial calibration unavailable: {exc}"

    def status(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "message": self.error,
            "file": str(self.path) if self.path else None,
            "reprojection_error_px": (
                self.transform.reprojection_error() if self.transform else None
            ),
            "image_size": (
                list(self.transform.image_size)
                if self.transform and self.transform.image_size
                else None
            ),
        }


calibration = CalibrationState()
