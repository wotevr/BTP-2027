"""
API contract tests.

These run against a real FastAPI TestClient with a throwaway SQLite database,
so they exercise the routes, the schema validation and the fusion engine
together.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.database.database import init_db
from app.main import app
from app.services.alert_service import alert_service
from app.services.fusion_engine import fusion_engine
from app.services.geofence_engine import geofence_engine
from app.services.homography import HomographyTransform, calibration
from app.services.worker_service import worker_service

IMAGE_PTS = [[60.0, 450.0], [580.0, 450.0], [430.0, 150.0], [210.0, 150.0]]
WORLD_PTS = [[0.0, 0.0], [30.0, 0.0], [30.0, 20.0], [0.0, 20.0]]

API = "/api/v1"


@pytest.fixture
def client(tmp_path, monkeypatch):
    init_db()
    fusion_engine.reset()
    worker_service.reset()
    alert_service.reset()
    geofence_engine.load([])

    with TestClient(app) as c:
        # Installed AFTER startup: the lifespan handler loads the (deliberately
        # absent) calibration file, which would otherwise clear this.
        calibration.transform = HomographyTransform.from_points(
            IMAGE_PTS, WORLD_PTS, image_size=(640, 480)
        )
        calibration.error = None
        yield c


def detection_payload(track_id=17, bbox=None, ppe=None, **kw):
    body = {
        "camera_id": "camera_01",
        "frame_id": 1,
        "frame_width": 640,
        "frame_height": 480,
        "workers": [
            {
                "track_id": track_id,
                "bbox": bbox or [300.0, 240.0, 345.0, 380.0],
                "confidence": 0.94,
                "ppe": ppe if ppe is not None else {"helmet": True, "vest": True},
            }
        ],
    }
    body.update(kw)
    return body


# --------------------------------------------------------------------------- #
# Health / docs
# --------------------------------------------------------------------------- #
def test_root_and_health(client):
    assert client.get("/health").json()["status"] == "ok"
    # Service info moved to /api/info so that / can serve the built dashboard.
    assert client.get("/api/info").json()["docs"] == "/docs"


def test_root_serves_the_dashboard_when_built(client):
    """
    / is the dashboard once frontend/dist exists, and falls back to service
    info when it does not. Both are valid; a 404 never is.
    """
    r = client.get("/")
    assert r.status_code == 200
    if "text/html" in r.headers.get("content-type", ""):
        assert "<div id=\"root\"" in r.text
    else:
        assert r.json()["docs"] == "/docs"


def test_openapi_schema_is_served(client):
    schema = client.get("/openapi.json").json()
    assert f"{API}/detections" in schema["paths"]
    assert f"{API}/zones" in schema["paths"]


# --------------------------------------------------------------------------- #
# Ingestion validation
# --------------------------------------------------------------------------- #
def test_accepts_a_valid_frame(client):
    r = client.post(f"{API}/detections", json=detection_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["processed_workers"] == 1
    assert body["spatial_calibration"] is True


def test_rejects_a_three_value_bbox(client):
    r = client.post(f"{API}/detections", json=detection_payload(bbox=[1.0, 2.0, 3.0]))
    assert r.status_code == 422
    assert "exactly 4" in r.text


def test_rejects_an_inverted_bbox(client):
    r = client.post(
        f"{API}/detections", json=detection_payload(bbox=[400.0, 400.0, 300.0, 300.0])
    )
    assert r.status_code == 422
    assert "degenerate bbox" in r.text


def test_rejects_a_negative_track_id(client):
    r = client.post(f"{API}/detections", json=detection_payload(track_id=-1))
    assert r.status_code == 422


def test_rejects_confidence_out_of_range(client):
    payload = detection_payload()
    payload["workers"][0]["confidence"] = 1.7
    assert client.post(f"{API}/detections", json=payload).status_code == 422


def test_rejects_duplicate_track_ids_in_one_frame(client):
    payload = detection_payload()
    payload["workers"].append(dict(payload["workers"][0]))
    r = client.post(f"{API}/detections", json=payload)
    assert r.status_code == 422
    assert "duplicate track_id" in r.text


def test_accepts_an_empty_frame(client):
    payload = detection_payload()
    payload["workers"] = []
    r = client.post(f"{API}/detections", json=payload)
    assert r.status_code == 200
    assert r.json()["processed_workers"] == 0


def test_ppe_accepts_the_tri_state(client):
    r = client.post(
        f"{API}/detections",
        json=detection_payload(ppe={"helmet": True, "vest": False, "mask": None}),
    )
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# Raw (upstream-format) ingestion
# --------------------------------------------------------------------------- #
def test_raw_endpoint_runs_the_adapter(client):
    r = client.post(
        f"{API}/detections/raw",
        json={
            "camera_id": "camera_01",
            "frame_id": 12482,
            "class_names": {"0": "Hardhat", "4": "NO-Safety Vest", "5": "Person"},
            "detections": [
                {"bbox": [300, 240, 345, 380], "conf": 0.94, "class_id": 5, "track_id": 17},
                {"bbox": [310, 243, 336, 258], "conf": 0.88, "class_id": 0, "track_id": None},
                {"bbox": [303, 272, 342, 320], "conf": 0.81, "class_id": 4, "track_id": None},
            ],
        },
    )
    assert r.status_code == 200
    assert r.json()["processed_workers"] == 1

    worker = client.get(f"{API}/workers/17").json()
    assert worker["live"]["ppe"]["helmet"] is True
    assert worker["live"]["ppe"]["vest"] is False


def test_raw_endpoint_rejects_a_missing_detections_list(client):
    r = client.post(f"{API}/detections/raw", json={"camera_id": "x"})
    assert r.status_code == 422
    assert "detections" in r.text


def test_raw_endpoint_rejects_bad_class_name_keys(client):
    r = client.post(
        f"{API}/detections/raw",
        json={"detections": [], "class_names": {"not-an-int": "Person"}},
    )
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Spatial result
# --------------------------------------------------------------------------- #
def test_worker_gets_world_coordinates(client):
    # Foot point of this box is (320, 450) - the near-centre of the site.
    client.post(
        f"{API}/detections", json=detection_payload(bbox=[300.0, 300.0, 340.0, 450.0])
    )
    state = client.get(f"{API}/state").json()
    pos = state["workers"][0]["world_position"]
    assert pos is not None
    assert 14 < pos["x"] < 16, f"expected mid-width, got {pos}"
    assert -0.5 < pos["y"] < 0.5, f"expected the near edge, got {pos}"


def test_system_degrades_without_calibration(client):
    calibration.transform = None
    calibration.error = "Spatial calibration unavailable: no homography file"

    r = client.post(f"{API}/detections", json=detection_payload(track_id=21))
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["spatial_calibration"] is False
    assert "unavailable" in body["detail"]

    worker = next(w for w in client.get(f"{API}/state").json()["workers"] if w["id"] == 21)
    assert worker["world_position"] is None
    assert worker["pixel_position"]["x"] > 0, "still tracked in pixel space"


# --------------------------------------------------------------------------- #
# Zones CRUD
# --------------------------------------------------------------------------- #
def test_zone_crud_round_trip(client):
    created = client.post(
        f"{API}/zones",
        json={
            "name": "Test Excavation",
            "zone_type": "EXCAVATION",
            "severity": "CRITICAL",
            "polygon": [[10, 5], [20, 5], [20, 15], [10, 15]],
            "description": "pytest",
        },
    )
    assert created.status_code == 201
    zone_id = created.json()["zone_id"]
    assert zone_id == "test_excavation"

    assert any(z["zone_id"] == zone_id for z in client.get(f"{API}/zones").json())

    updated = client.put(f"{API}/zones/{zone_id}", json={"severity": "LOW"})
    assert updated.status_code == 200
    assert updated.json()["severity"] == "LOW"

    assert client.delete(f"{API}/zones/{zone_id}").status_code == 200
    assert not any(z["zone_id"] == zone_id for z in client.get(f"{API}/zones").json())


def test_zone_rejects_fewer_than_three_vertices(client):
    r = client.post(
        f"{API}/zones", json={"name": "Bad", "polygon": [[0, 0], [1, 1]]}
    )
    assert r.status_code == 422
    assert "at least 3" in r.text


def test_zone_rejects_a_collinear_polygon(client):
    r = client.post(
        f"{API}/zones", json={"name": "Flat", "polygon": [[0, 0], [5, 5], [10, 10]]}
    )
    assert r.status_code == 422
    assert "degenerate" in r.text


def test_zone_rejects_a_duplicate_id(client):
    body = {"name": "Dup Zone", "polygon": [[0, 0], [4, 0], [4, 4]], "zone_id": "dup"}
    assert client.post(f"{API}/zones", json=body).status_code == 201
    assert client.post(f"{API}/zones", json=body).status_code == 409
    client.delete(f"{API}/zones/dup")


def test_updating_a_missing_zone_is_404(client):
    assert client.put(f"{API}/zones/nope", json={"severity": "LOW"}).status_code == 404
    assert client.delete(f"{API}/zones/nope").status_code == 404


def test_new_zone_takes_effect_immediately(client):
    """A zone created through the API must geofence on the very next frame."""
    client.post(
        f"{API}/zones",
        json={
            "zone_id": "live_zone",
            "name": "Live Zone",
            "severity": "HIGH",
            "polygon": [[13, -1], [17, -1], [17, 3], [13, 3]],
        },
    )
    client.post(
        f"{API}/detections",
        json=detection_payload(track_id=55, bbox=[300.0, 300.0, 340.0, 450.0]),
    )
    state = client.get(f"{API}/state").json()
    worker = next(w for w in state["workers"] if w["id"] == 55)
    assert "live_zone" in worker["zones"]
    client.delete(f"{API}/zones/live_zone")


# --------------------------------------------------------------------------- #
# Workers, alerts, analytics
# --------------------------------------------------------------------------- #
def test_workers_list_and_detail(client):
    client.post(f"{API}/detections", json=detection_payload(track_id=31))
    workers = client.get(f"{API}/workers").json()
    assert any(w["worker_id"] == 31 for w in workers)

    detail = client.get(f"{API}/workers/31").json()
    assert detail["worker"]["worker_id"] == 31
    assert detail["live"]["id"] == 31
    assert isinstance(detail["trajectory"], list)


def test_unknown_worker_is_404(client):
    assert client.get(f"{API}/workers/999999").status_code == 404


def test_alerts_endpoints(client):
    assert client.get(f"{API}/alerts").status_code == 200
    assert client.get(f"{API}/alerts/active").json() == []
    assert client.post(f"{API}/alerts/nosuchevent/resolve").status_code == 404


def test_analytics_shape(client):
    client.post(f"{API}/detections", json=detection_payload(track_id=41))
    body = client.get(f"{API}/analytics").json()
    for key in ("totals", "alerts_by_type", "timeline", "zone_occupancy"):
        assert key in body
    assert body["totals"]["active_workers"] >= 1


def test_calibration_endpoints(client):
    body = client.get(f"{API}/calibration").json()
    assert body["available"] is True
    assert len(body["homography"]) == 3

    projected = client.post(f"{API}/calibration/project", json={"pixel": [320, 450]})
    assert projected.status_code == 200
    assert projected.json()["world"][1] == pytest.approx(0.0, abs=0.2)

    bad = client.post(
        f"{API}/calibration/preview",
        json={"image_points": [[0, 0], [1, 1], [2, 2], [3, 3]], "world_points": WORLD_PTS},
    )
    assert bad.status_code == 422


# --------------------------------------------------------------------------- #
# WebSocket
# --------------------------------------------------------------------------- #
def test_websocket_sends_a_snapshot_on_connect(client):
    with client.websocket_connect("/ws/live") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "site_state"
        for key in ("workers", "alerts", "zones", "calibration", "site"):
            assert key in msg


def test_websocket_ping_pong_and_bad_input(client):
    with client.websocket_connect("/ws/live") as ws:
        ws.receive_json()

        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"

        ws.send_text("this is not json")
        assert "not valid JSON" in ws.receive_json()["message"]

        ws.send_json({"type": "nonsense"})
        assert "unknown message type" in ws.receive_json()["message"]

        ws.send_json({"type": "state"})
        assert ws.receive_json()["type"] == "site_state"


def test_websocket_receives_live_updates(client):
    with client.websocket_connect("/ws/live") as ws:
        ws.receive_json()  # initial snapshot
        client.post(f"{API}/detections", json=detection_payload(track_id=77))
        msg = ws.receive_json()
        assert msg["type"] == "site_state"
        assert any(w["id"] == 77 for w in msg["workers"])


# --------------------------------------------------------------------------- #
# Demo mode
# --------------------------------------------------------------------------- #
def test_demo_mode_start_and_stop(client):
    started = client.post(f"{API}/demo/start").json()
    assert started["demo_mode"] is True
    assert len(started["workers"]) == 5

    assert client.get(f"{API}/system/status").json()["demo_mode"] is True

    stopped = client.post(f"{API}/demo/stop").json()
    assert stopped["demo_mode"] is False
