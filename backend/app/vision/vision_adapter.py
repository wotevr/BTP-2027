"""
THE INTEGRATION BOUNDARY.

Upstream (https://github.com/darkm4tter07/Construction_Safety_BTP) owns:
    video -> YOLO11 -> person + PPE detection -> BoT-SORT -> persistent track ids

This module is the ONLY place in BTP-2027 that knows what that output looks
like. Everything downstream consumes `app.schemas.detection.DetectionFrame`
and never imports ultralytics, torch, or any tracker internals.

WHAT UPSTREAM ACTUALLY PRODUCES
-------------------------------
`YOLODetector.detect()` returns a flat list of per-class boxes:

    {"bbox": [x1, y1, x2, y2], "conf": 0.94, "class_id": 5, "track_id": 17}

in a 640x480 pixel space (the pipeline resizes every frame before inference).

Trained classes, read directly from the committed weights:

    yolo11n.pt (10 classes)
        0 Hardhat   1 Mask   2 NO-Hardhat   3 NO-Mask   4 NO-Safety Vest
        5 Person    6 Safety Cone          7 Safety Vest
        8 machinery 9 vehicle

    yolo11s.pt (11 classes) - same 0..8, then
        9 utility pole      10 vehicle

Because the two variants disagree above index 8, this adapter maps by class
NAME wherever the caller can supply `model.names`, and only falls back to an
index table when it cannot.

THE ONE GAP WE HAVE TO CLOSE
----------------------------
Upstream detects PPE as INDEPENDENT boxes. It never says which helmet belongs
to which person - the existing dashboard only counts them globally. So a
per-worker `ppe` record does not exist upstream and cannot simply be copied.

This adapter derives it by SPATIAL ASSOCIATION of the boxes that upstream
already produced. No second model, no retraining, no extra inference: it is
pure geometry over the existing detections. The method and its limits are
documented in `associate_ppe` below and in the README.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from app.schemas.detection import DetectionFrame, PPEStatus, WorkerDetection

log = logging.getLogger(__name__)

PERSON = "PERSON"
CONTEXT = "CONTEXT"

# Canonical PPE keys used everywhere downstream.
HELMET, VEST, MASK = "helmet", "vest", "mask"


def normalize_class_name(name: str) -> str:
    """`NO-Safety Vest` -> `no safety vest`."""
    s = str(name).strip().lower()
    s = re.sub(r"[-_/]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# normalised class name -> (kind, ppe_item, present)
CLASS_ALIASES: dict[str, tuple[str, str | None, bool | None]] = {
    "person": (PERSON, None, None),
    "worker": (PERSON, None, None),
    # positives
    "hardhat": ("PPE", HELMET, True),
    "hard hat": ("PPE", HELMET, True),
    "helmet": ("PPE", HELMET, True),
    "safety helmet": ("PPE", HELMET, True),
    "mask": ("PPE", MASK, True),
    "face mask": ("PPE", MASK, True),
    "safety vest": ("PPE", VEST, True),
    "vest": ("PPE", VEST, True),
    "reflective vest": ("PPE", VEST, True),
    # explicit negatives
    "no hardhat": ("PPE", HELMET, False),
    "no hard hat": ("PPE", HELMET, False),
    "no helmet": ("PPE", HELMET, False),
    "no mask": ("PPE", MASK, False),
    "no safety vest": ("PPE", VEST, False),
    "no vest": ("PPE", VEST, False),
    # site context - detected upstream, not PPE, kept for future use
    "safety cone": (CONTEXT, None, None),
    "machinery": (CONTEXT, None, None),
    "vehicle": (CONTEXT, None, None),
    "utility pole": (CONTEXT, None, None),
}

# Fallback index tables, used only when `model.names` is unavailable.
DEFAULT_CLASS_NAMES_10 = {
    0: "Hardhat", 1: "Mask", 2: "NO-Hardhat", 3: "NO-Mask", 4: "NO-Safety Vest",
    5: "Person", 6: "Safety Cone", 7: "Safety Vest", 8: "machinery", 9: "vehicle",
}
DEFAULT_CLASS_NAMES_11 = {
    0: "Hardhat", 1: "Mask", 2: "NO-Hardhat", 3: "NO-Mask", 4: "NO-Safety Vest",
    5: "Person", 6: "Safety Cone", 7: "Safety Vest", 8: "machinery",
    9: "utility pole", 10: "vehicle",
}


def resolve_class_names(
    names: Mapping[int, str] | Sequence[str] | None, max_class_id: int = 9
) -> dict[int, str]:
    """
    Normalise whatever the caller has into {index: name}.

    Accepts the ultralytics `model.names` dict, a plain list, or None (in which
    case the 10- or 11-class default table is chosen by the highest class id
    actually seen in the frame).
    """
    if isinstance(names, Mapping) and names:
        return {int(k): str(v) for k, v in names.items()}
    if isinstance(names, (list, tuple)) and names:
        return {i: str(v) for i, v in enumerate(names)}
    return DEFAULT_CLASS_NAMES_11 if max_class_id >= 10 else DEFAULT_CLASS_NAMES_10


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
def _area(b: Sequence[float]) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _intersection_area(a: Sequence[float], b: Sequence[float]) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def containment(inner: Sequence[float], outer: Sequence[float]) -> float:
    """Fraction of `inner` that falls inside `outer` (0..1)."""
    a = _area(inner)
    return 0.0 if a <= 0 else _intersection_area(inner, outer) / a


# Where each item is expected to sit, as a fraction of the person box height
# measured from the top. A helmet low on the legs is almost certainly a
# neighbouring worker leaking into this box.
EXPECTED_BAND: dict[str, tuple[float, float]] = {
    HELMET: (0.00, 0.45),
    MASK: (0.00, 0.50),
    VEST: (0.10, 0.85),
}


def _band_score(item: str, ppe_box: Sequence[float], person_box: Sequence[float]) -> float:
    """1.0 when the item sits where it should, tapering to 0.2 when it does not."""
    top, bottom = person_box[1], person_box[3]
    height = bottom - top
    if height <= 0:
        return 0.0
    cy = (ppe_box[1] + ppe_box[3]) / 2.0
    frac = (cy - top) / height
    lo, hi = EXPECTED_BAND.get(item, (0.0, 1.0))
    if lo <= frac <= hi:
        return 1.0
    distance = lo - frac if frac < lo else frac - hi
    return max(0.2, 1.0 - 2.5 * distance)


# Minimum fraction of a PPE box that must fall inside a person box before we
# are willing to say the item belongs to that person.
MIN_CONTAINMENT = 0.45


@dataclass
class RawDetection:
    """One upstream box, already decoded."""

    bbox: list[float]
    confidence: float
    class_id: int
    track_id: int | None
    kind: str
    ppe_item: str | None
    ppe_present: bool | None


def decode_detections(
    detections: Sequence[Mapping[str, Any]],
    class_names: Mapping[int, str] | Sequence[str] | None = None,
) -> list[RawDetection]:
    """Turn the upstream dicts into typed records, dropping anything malformed."""
    max_cid = 0
    for d in detections:
        try:
            max_cid = max(max_cid, int(d.get("class_id", 0)))
        except (TypeError, ValueError):
            continue
    names = resolve_class_names(class_names, max_cid)

    out: list[RawDetection] = []
    for d in detections:
        bbox = d.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            log.debug("dropping detection with bad bbox: %r", d)
            continue
        try:
            box = [float(c) for c in bbox]
            cid = int(d.get("class_id", -1))
        except (TypeError, ValueError):
            log.debug("dropping detection with non-numeric fields: %r", d)
            continue
        if box[2] <= box[0] or box[3] <= box[1]:
            log.debug("dropping degenerate bbox: %r", box)
            continue

        raw_name = names.get(cid)
        if raw_name is None:
            log.debug("unknown class id %s (names has %d entries)", cid, len(names))
            continue
        kind, item, present = CLASS_ALIASES.get(
            normalize_class_name(raw_name), (CONTEXT, None, None)
        )

        tid = d.get("track_id")
        try:
            tid = int(tid) if tid is not None else None
        except (TypeError, ValueError):
            tid = None

        # `conf` is the upstream key; `confidence` is accepted too.
        conf = d.get("conf", d.get("confidence", 1.0))
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = 1.0

        out.append(
            RawDetection(
                bbox=box,
                confidence=min(max(conf, 0.0), 1.0),
                class_id=cid,
                track_id=tid,
                kind=kind,
                ppe_item=item,
                ppe_present=present,
            )
        )
    return out


def associate_ppe(
    person: RawDetection, ppe_boxes: Sequence[RawDetection]
) -> PPEStatus:
    """
    Assign PPE boxes to one person box.

    For every PPE box we compute

        score = containment(ppe, person) * band_score(item) * detector_confidence

    and keep the highest-scoring box per item. A positive class (`Hardhat`) and
    a negative class (`NO-Hardhat`) compete on the same scale, so whichever the
    detector was more sure about wins. An item nothing was found for stays
    `None` (unknown) rather than being guessed as missing.

    LIMITS - state these in the viva rather than hiding them:
      * With heavy occlusion or workers standing shoulder to shoulder, a box can
        be attributed to the wrong person. MIN_CONTAINMENT and the vertical band
        check reduce, but do not eliminate, this.
      * The upstream model emits the `NO-*` classes itself, so "missing" is the
        judgement of the detector, not an inference drawn from absence.
    """
    best: dict[str, tuple[float, bool]] = {}

    for ppe in ppe_boxes:
        item = ppe.ppe_item
        if item is None:
            continue
        score = association_score(person, ppe)
        if score <= 0:
            continue
        current = best.get(item)
        if current is None or score > current[0]:
            best[item] = (score, bool(ppe.ppe_present))

    return PPEStatus(**{item: value for item, (_score, value) in best.items()})


def association_score(person: RawDetection, ppe: RawDetection) -> float:
    """Confidence that this PPE box belongs to this person. 0 means no."""
    if ppe.ppe_item is None:
        return 0.0
    cover = containment(ppe.bbox, person.bbox)
    if cover < MIN_CONTAINMENT:
        return 0.0
    return (
        cover
        * _band_score(ppe.ppe_item, ppe.bbox, person.bbox)
        * max(ppe.confidence, 0.01)
    )


def associate_ppe_frame(
    persons: Sequence[RawDetection], ppe_boxes: Sequence[RawDetection]
) -> list[PPEStatus]:
    """
    Assign PPE across ALL people in the frame at once, exclusively.

    Doing each person independently lets one helmet be claimed by two
    overlapping workers, which is exactly the situation on a crowded site. So
    instead: score every (person, ppe) pair, sort the pairs by score, and walk
    the list greedily - each PPE box is consumed by the first (best-scoring)
    person that still needs that item.

    This is the classic greedy approximation to a bipartite assignment. A full
    Hungarian solve would be marginally better, but at three PPE items and a
    handful of people per frame the difference is not measurable and greedy
    keeps the code readable.
    """
    results: list[dict[str, bool]] = [{} for _ in persons]
    if not persons or not ppe_boxes:
        return [PPEStatus(**r) for r in results]

    pairs: list[tuple[float, int, int]] = []
    for pi, person in enumerate(persons):
        for bi, ppe in enumerate(ppe_boxes):
            score = association_score(person, ppe)
            if score > 0:
                pairs.append((score, pi, bi))

    # Highest score first; ties broken deterministically by index.
    pairs.sort(key=lambda t: (-t[0], t[1], t[2]))

    used_boxes: set[int] = set()
    for _score, pi, bi in pairs:
        if bi in used_boxes:
            continue
        item = ppe_boxes[bi].ppe_item
        if item is None or item in results[pi]:
            continue
        results[pi][item] = bool(ppe_boxes[bi].ppe_present)
        used_boxes.add(bi)

    return [PPEStatus(**r) for r in results]


class VisionAdapter:
    """Stateless translator: upstream detections -> DetectionFrame."""

    def __init__(self, camera_id: str = "camera_01", frame_size: tuple[int, int] = (640, 480)):
        self.camera_id = camera_id
        self.frame_width, self.frame_height = frame_size

    def normalize(
        self,
        detections: Sequence[Mapping[str, Any]],
        class_names: Mapping[int, str] | Sequence[str] | None = None,
        frame_id: int = 0,
        timestamp: datetime | None = None,
        frame_size: tuple[int, int] | None = None,
        camera_id: str | None = None,
        source: str = "live",
    ) -> DetectionFrame:
        decoded = decode_detections(detections, class_names)

        persons = [d for d in decoded if d.kind == PERSON]
        ppe_boxes = [d for d in decoded if d.kind == "PPE"]

        # Assign PPE across the whole frame so no box is claimed twice.
        ppe_per_person = associate_ppe_frame(persons, ppe_boxes)

        workers: list[WorkerDetection] = []
        untracked = 0
        for p, ppe in zip(persons, ppe_per_person):
            if p.track_id is None:
                # The tracker had not confirmed this person yet. Without a stable
                # id we cannot do attendance or enter/exit events, so skip it
                # rather than inventing an id of our own.
                untracked += 1
                continue
            try:
                workers.append(
                    WorkerDetection(
                        track_id=p.track_id,
                        bbox=p.bbox,
                        confidence=p.confidence,
                        ppe=ppe,
                    )
                )
            except ValueError as exc:
                log.debug("skipping person track %s: %s", p.track_id, exc)

        if untracked:
            log.debug("%d person box(es) had no track id yet", untracked)

        # A tracker can briefly emit the same id twice; keep the more confident.
        deduped: dict[int, WorkerDetection] = {}
        for w in workers:
            prev = deduped.get(w.track_id)
            if prev is None or w.confidence > prev.confidence:
                deduped[w.track_id] = w

        w, h = frame_size or (self.frame_width, self.frame_height)
        return DetectionFrame(
            timestamp=timestamp or datetime.now(timezone.utc),
            camera_id=camera_id or self.camera_id,
            frame_id=frame_id,
            frame_width=w,
            frame_height=h,
            source="demo" if source == "demo" else "live",
            workers=list(deduped.values()),
        )


vision_adapter = VisionAdapter()
