"""
The integration boundary: upstream YOLO output -> normalised DetectionFrame.

These tests use the REAL class ids of the upstream weights:
    0 Hardhat  1 Mask  2 NO-Hardhat  3 NO-Mask  4 NO-Safety Vest
    5 Person   6 Safety Cone  7 Safety Vest  8 machinery  9 vehicle
"""
from __future__ import annotations

import pytest

from app.schemas.detection import DetectionFrame, WorkerDetection
from app.vision.vision_adapter import (
    DEFAULT_CLASS_NAMES_10,
    DEFAULT_CLASS_NAMES_11,
    VisionAdapter,
    associate_ppe_frame,
    containment,
    decode_detections,
    normalize_class_name,
    resolve_class_names,
)

PERSON_BOX = [300.0, 200.0, 360.0, 400.0]  # 60 wide, 200 tall


def person(track_id=17, bbox=None, conf=0.94):
    return {"bbox": bbox or PERSON_BOX, "conf": conf, "class_id": 5, "track_id": track_id}


def hardhat(present=True, box=None):
    return {
        "bbox": box or [312.0, 204.0, 348.0, 228.0],
        "conf": 0.88,
        "class_id": 0 if present else 2,
        "track_id": None,
    }


def vest(present=True, box=None):
    return {
        "bbox": box or [305.0, 250.0, 355.0, 310.0],
        "conf": 0.86,
        "class_id": 7 if present else 4,
        "track_id": None,
    }


def mask(present=True, box=None):
    return {
        "bbox": box or [318.0, 226.0, 342.0, 244.0],
        "conf": 0.74,
        "class_id": 1 if present else 3,
        "track_id": None,
    }


@pytest.fixture
def adapter():
    return VisionAdapter(camera_id="camera_01", frame_size=(640, 480))


# --------------------------------------------------------------------------- #
# Class mapping
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("NO-Safety Vest", "no safety vest"),
        ("Hardhat", "hardhat"),
        ("  Safety   Vest  ", "safety vest"),
        ("NO_Hardhat", "no hardhat"),
        ("utility pole", "utility pole"),
    ],
)
def test_class_name_normalisation(raw, expected):
    assert normalize_class_name(raw) == expected


def test_default_table_picks_10_or_11_class_variant():
    """The two shipped weights disagree above index 8, so the count matters."""
    assert resolve_class_names(None, max_class_id=9) == DEFAULT_CLASS_NAMES_10
    assert resolve_class_names(None, max_class_id=10) == DEFAULT_CLASS_NAMES_11
    assert resolve_class_names(None, 9)[9] == "vehicle"
    assert resolve_class_names(None, 10)[9] == "utility pole"
    assert resolve_class_names(None, 10)[10] == "vehicle"


def test_explicit_model_names_win_over_the_default_table():
    names = resolve_class_names({0: "Helmet", 1: "Person"}, max_class_id=1)
    assert names == {0: "Helmet", 1: "Person"}


def test_names_accepted_as_a_plain_list():
    assert resolve_class_names(["Hardhat", "Person"], 1) == {0: "Hardhat", 1: "Person"}


# --------------------------------------------------------------------------- #
# Decoding and robustness
# --------------------------------------------------------------------------- #
def test_decode_splits_persons_from_ppe():
    decoded = decode_detections([person(), hardhat(), vest(False)])
    kinds = sorted(d.kind for d in decoded)
    assert kinds == ["PERSON", "PPE", "PPE"]


def test_decode_drops_malformed_boxes():
    bad = [
        {"bbox": [1, 2, 3], "conf": 0.9, "class_id": 5, "track_id": 1},      # 3 values
        {"bbox": "nope", "conf": 0.9, "class_id": 5, "track_id": 2},         # not a list
        {"bbox": [10, 10, 5, 5], "conf": 0.9, "class_id": 5, "track_id": 3},  # inverted
        {"bbox": [0, 0, 10, 10], "conf": 0.9, "class_id": 99, "track_id": 4},  # unknown class
        person(track_id=5),                                                   # the good one
    ]
    decoded = decode_detections(bad)
    assert [d.track_id for d in decoded] == [5]


def test_decode_accepts_both_conf_and_confidence_keys():
    d = decode_detections([{"bbox": PERSON_BOX, "confidence": 0.5, "class_id": 5, "track_id": 1}])
    assert d[0].confidence == pytest.approx(0.5)


def test_context_classes_are_not_treated_as_ppe():
    cone = {"bbox": [10.0, 10.0, 30.0, 40.0], "conf": 0.7, "class_id": 6, "track_id": None}
    machinery = {"bbox": [50.0, 50.0, 90.0, 90.0], "conf": 0.7, "class_id": 8, "track_id": None}
    decoded = decode_detections([cone, machinery])
    assert all(d.kind == "CONTEXT" and d.ppe_item is None for d in decoded)


# --------------------------------------------------------------------------- #
# PPE association
# --------------------------------------------------------------------------- #
def test_containment_fraction():
    inner = [0.0, 0.0, 10.0, 10.0]
    assert containment(inner, [0.0, 0.0, 10.0, 10.0]) == pytest.approx(1.0)
    assert containment(inner, [5.0, 0.0, 15.0, 10.0]) == pytest.approx(0.5)
    assert containment(inner, [100.0, 100.0, 110.0, 110.0]) == pytest.approx(0.0)


def test_associates_positive_ppe_to_the_person(adapter):
    frame = adapter.normalize([person(), hardhat(True), vest(True), mask(True)])
    ppe = frame.workers[0].ppe
    assert (ppe.helmet, ppe.vest, ppe.mask) == (True, True, True)


def test_associates_negative_ppe_classes(adapter):
    frame = adapter.normalize([person(), hardhat(False), vest(False)])
    ppe = frame.workers[0].ppe
    assert ppe.helmet is False
    assert ppe.vest is False
    assert ppe.mask is None, "nothing was observed for the mask, so it is unknown"


def test_unobserved_ppe_is_unknown_not_missing(adapter):
    """Absence of evidence must never become a violation."""
    frame = adapter.normalize([person()])
    ppe = frame.workers[0].ppe
    assert (ppe.helmet, ppe.vest, ppe.mask) == (None, None, None)
    assert ppe.missing_items(["helmet", "vest"]) == []


def test_ppe_far_from_any_person_is_ignored(adapter):
    stray = hardhat(True, box=[10.0, 10.0, 40.0, 30.0])
    frame = adapter.normalize([person(), stray])
    assert frame.workers[0].ppe.helmet is None


def test_helmet_at_ankle_height_is_down_weighted():
    """A hardhat box at the feet belongs to someone else, not this person."""
    from app.vision.vision_adapter import association_score

    decoded = decode_detections([person(), hardhat(True, box=[312.0, 370.0, 348.0, 394.0])])
    p, low_hat = decoded[0], decoded[1]
    decoded_high = decode_detections([person(), hardhat(True)])
    high_hat = decoded_high[1]
    assert association_score(p, low_hat) < association_score(p, high_hat)


def test_a_ppe_box_is_claimed_by_only_one_person():
    """
    Two overlapping people and a single hardhat: exactly one of them gets it.
    Without exclusive assignment both would be reported as wearing a helmet.
    """
    a = person(track_id=1, bbox=[300.0, 200.0, 360.0, 400.0])
    b = person(track_id=2, bbox=[310.0, 200.0, 370.0, 400.0])
    decoded = decode_detections([a, b, hardhat(True)])
    persons = [d for d in decoded if d.kind == "PERSON"]
    ppes = [d for d in decoded if d.kind == "PPE"]

    results = associate_ppe_frame(persons, ppes)
    helmets = [r.helmet for r in results]
    assert helmets.count(True) == 1
    assert helmets.count(None) == 1


def test_ppe_labels_for_the_ui(adapter):
    frame = adapter.normalize([person(), hardhat(True), vest(False)])
    labels = frame.workers[0].ppe.as_labels()
    assert labels["helmet"] == "OK"
    assert labels["vest"] == "MISSING"
    assert labels["mask"] == "UNKNOWN"


# --------------------------------------------------------------------------- #
# Track ids
# --------------------------------------------------------------------------- #
def test_untracked_person_is_skipped(adapter):
    """No stable id means no attendance and no enter/exit - so we drop it."""
    frame = adapter.normalize([person(track_id=None), person(track_id=9)])
    assert [w.track_id for w in frame.workers] == [9]


def test_duplicate_track_id_keeps_the_more_confident_box(adapter):
    a = person(track_id=7, conf=0.4)
    b = person(track_id=7, bbox=[100.0, 100.0, 140.0, 220.0], conf=0.9)
    frame = adapter.normalize([a, b])
    assert len(frame.workers) == 1
    assert frame.workers[0].confidence == pytest.approx(0.9)


def test_upstream_track_ids_are_passed_through_unchanged(adapter):
    ids = [3, 17, 42, 108]
    frame = adapter.normalize([person(track_id=i) for i in ids])
    assert sorted(w.track_id for w in frame.workers) == ids


def test_frame_metadata_is_carried(adapter):
    frame = adapter.normalize(
        [person()], frame_id=12482, camera_id="cam_07", frame_size=(1280, 720)
    )
    assert frame.frame_id == 12482
    assert frame.camera_id == "cam_07"
    assert (frame.frame_width, frame.frame_height) == (1280, 720)
    assert frame.source == "live"


def test_empty_frame_is_valid(adapter):
    frame = adapter.normalize([])
    assert isinstance(frame, DetectionFrame)
    assert frame.workers == []


# --------------------------------------------------------------------------- #
# Foot point
# --------------------------------------------------------------------------- #
def test_foot_point_is_bottom_centre():
    w = WorkerDetection(track_id=1, bbox=[412.0, 183.0, 498.0, 421.0])
    assert w.foot_point == (455.0, 421.0)


def test_foot_point_is_not_the_box_centre():
    w = WorkerDetection(track_id=1, bbox=[0.0, 0.0, 100.0, 200.0])
    centre_y = 100.0
    assert w.foot_point == (50.0, 200.0)
    assert w.foot_point[1] != centre_y
