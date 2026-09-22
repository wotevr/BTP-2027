"""Homography: construction, validation, and the pixel/world round trip."""
from __future__ import annotations

import json

import pytest

from app.services.homography import HomographyError, HomographyTransform

# A camera looking across a 30m x 20m site from the Y=0 edge.
IMAGE_PTS = [[60.0, 450.0], [580.0, 450.0], [430.0, 150.0], [210.0, 150.0]]
WORLD_PTS = [[0.0, 0.0], [30.0, 0.0], [30.0, 20.0], [0.0, 20.0]]


@pytest.fixture
def transform() -> HomographyTransform:
    return HomographyTransform.from_points(IMAGE_PTS, WORLD_PTS, image_size=(640, 480))


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #
def test_builds_from_four_points(transform):
    assert transform.matrix.shape == (3, 3)
    assert transform.image_size == (640, 480)


def test_calibration_points_map_to_themselves(transform):
    """The defining correspondences must come back exactly."""
    for (u, v), (x, y) in zip(IMAGE_PTS, WORLD_PTS):
        wx, wy = transform.pixel_to_world(u, v)
        assert wx == pytest.approx(x, abs=1e-6)
        assert wy == pytest.approx(y, abs=1e-6)


def test_reprojection_error_is_negligible(transform):
    assert transform.reprojection_error() < 1e-6


# --------------------------------------------------------------------------- #
# pixel <-> world
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "world", [(0, 0), (30, 0), (30, 20), (0, 20), (15, 10), (14.2, 8.7), (1.5, 19.1)]
)
def test_round_trip_world_pixel_world(transform, world):
    u, v = transform.world_to_pixel(*world)
    back = transform.pixel_to_world(u, v)
    assert back[0] == pytest.approx(world[0], abs=1e-6)
    assert back[1] == pytest.approx(world[1], abs=1e-6)


def test_perspective_is_not_linear(transform):
    """
    Equal steps in world depth must NOT produce equal steps in pixels - if they
    did, the homography would be a plain affine scale and the whole point of
    using a projective transform would be lost.
    """
    _, v0 = transform.world_to_pixel(15, 0)
    _, v10 = transform.world_to_pixel(15, 10)
    _, v20 = transform.world_to_pixel(15, 20)
    near_span = abs(v0 - v10)
    far_span = abs(v10 - v20)
    assert near_span > far_span * 1.2, "far half of the site should compress"


def test_batch_matches_single(transform):
    pts = [[100.0, 400.0], [320.0, 300.0], [500.0, 460.0]]
    batch = transform.pixels_to_world(pts)
    for (u, v), got in zip(pts, batch):
        assert got == pytest.approx(transform.pixel_to_world(u, v), abs=1e-9)


def test_scale_pixel_rescales_to_calibration_size(transform):
    """A 1280x960 detection is half-scaled into the 640x480 calibration space."""
    u, v = transform.scale_pixel(640.0, 480.0, width=1280, height=960)
    assert (u, v) == pytest.approx((320.0, 240.0))


def test_scale_pixel_is_a_no_op_at_matching_size(transform):
    assert transform.scale_pixel(320.0, 240.0, 640, 480) == (320.0, 240.0)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def test_rejects_fewer_than_four_points():
    with pytest.raises(HomographyError, match="at least 4"):
        HomographyTransform.from_points(IMAGE_PTS[:3], WORLD_PTS[:3])


def test_rejects_mismatched_counts():
    with pytest.raises(HomographyError, match="same length"):
        HomographyTransform.from_points(IMAGE_PTS, WORLD_PTS[:3] + [[1, 1], [2, 2]])


def test_rejects_collinear_image_points():
    collinear = [[0.0, 0.0], [10.0, 10.0], [20.0, 20.0], [30.0, 30.0]]
    with pytest.raises(HomographyError, match="collinear"):
        HomographyTransform.from_points(collinear, WORLD_PTS)


def test_rejects_duplicate_points():
    dup = [[60.0, 450.0], [60.0, 450.0], [430.0, 150.0], [210.0, 150.0]]
    with pytest.raises(HomographyError, match="same point"):
        HomographyTransform.from_points(dup, WORLD_PTS)


def test_rejects_non_numeric():
    bad = [["a", "b"], [580.0, 450.0], [430.0, 150.0], [210.0, 150.0]]
    with pytest.raises(HomographyError, match="non-numeric"):
        HomographyTransform.from_points(bad, WORLD_PTS)


def test_rejects_nan():
    bad = [[float("nan"), 450.0], [580.0, 450.0], [430.0, 150.0], [210.0, 150.0]]
    with pytest.raises(HomographyError, match="NaN"):
        HomographyTransform.from_points(bad, WORLD_PTS)


def test_rejects_wrong_matrix_shape():
    with pytest.raises(HomographyError, match="3x3"):
        HomographyTransform([[1, 0], [0, 1]])


def test_rejects_singular_matrix():
    with pytest.raises(HomographyError, match="singular"):
        HomographyTransform([[1, 2, 3], [2, 4, 6], [3, 6, 9]])


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def test_save_and_load_round_trip(transform, tmp_path):
    path = transform.save(tmp_path / "calib" / "homography.json")
    assert path.exists()

    loaded = HomographyTransform.load(path)
    assert loaded.image_size == (640, 480)
    for world in [(0, 0), (15, 10), (30, 20)]:
        assert loaded.world_to_pixel(*world) == pytest.approx(
            transform.world_to_pixel(*world), abs=1e-9
        )


def test_load_recomputes_when_only_points_were_stored(tmp_path):
    p = tmp_path / "points.json"
    p.write_text(
        json.dumps({"image_points": IMAGE_PTS, "world_points": WORLD_PTS}),
        encoding="utf-8",
    )
    loaded = HomographyTransform.load(p)
    assert loaded.pixel_to_world(60.0, 450.0) == pytest.approx((0.0, 0.0), abs=1e-6)


def test_load_missing_file_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        HomographyTransform.load(tmp_path / "nope.json")


def test_load_rejects_bad_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(HomographyError, match="not valid JSON"):
        HomographyTransform.load(p)


def test_load_rejects_file_without_usable_content(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"note": "nothing here"}), encoding="utf-8")
    with pytest.raises(HomographyError, match="neither"):
        HomographyTransform.load(p)
