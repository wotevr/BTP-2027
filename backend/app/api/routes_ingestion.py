"""
Ingestion: the door the vision layer knocks on.

POST /api/v1/detections is the ONLY way detections enter the system, whether
they come from the upstream YOLO bridge, from a recorded file, or from demo
mode. Keeping a single entrance is what makes the demo genuinely exercise the
production path.
"""
from __future__ import annotations

import base64
import binascii
import logging

from fastapi import APIRouter, Body, HTTPException, status

from app.core.config import settings
from app.core.websocket_manager import manager
from app.schemas.detection import DetectionFrame, IngestionResponse
from app.services.demo_simulator import demo_simulator
from app.services.frame_buffer import frame_buffer
from app.services.fusion_engine import fusion_engine
from app.services.homography import calibration
from app.services.persistence import db_writer
from app.vision.vision_adapter import vision_adapter

log = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/detections",
    response_model=IngestionResponse,
    summary="Ingest one frame of normalised detections",
)
async def ingest_detections(frame: DetectionFrame) -> IngestionResponse:
    """
    Accepts one frame from the vision layer and runs the full spatial pipeline:
    foot point, homography, geofencing, PPE rules, attendance, telemetry,
    safety events and WebSocket broadcast.

    Bounding boxes are in the pixel space given by `frame_width`/`frame_height`
    (defaults to the 640x480 the upstream pipeline runs inference at).
    """
    try:
        return await fusion_engine.process_frame(frame)
    except Exception as exc:  # noqa: BLE001
        log.exception("Frame processing failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"frame processing failed: {exc}",
        ) from exc


@router.post(
    "/detections/raw",
    response_model=IngestionResponse,
    summary="Ingest detections straight out of the upstream YOLO pipeline",
)
async def ingest_raw(payload: dict = Body(...)) -> IngestionResponse:
    """
    Convenience endpoint for wiring up the existing repository with the least
    possible change on its side.

    Send exactly what `YOLODetector.detect()` returned, and this endpoint runs
    it through the vision adapter for you:

        {
          "camera_id": "camera_01",
          "frame_id": 12482,
          "frame_width": 640,
          "frame_height": 480,
          "class_names": {"0": "Hardhat", "5": "Person", ...},
          "detections": [
            {"bbox": [412, 183, 498, 421], "conf": 0.94,
             "class_id": 5, "track_id": 17}
          ]
        }

    `class_names` is optional but recommended - pass `model.names` so that the
    10-class and 11-class weights are both mapped correctly.
    """
    detections = payload.get("detections")
    if not isinstance(detections, list):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="body must contain a 'detections' list",
        )

    # Optional annotated frame for the live camera view. Purely a display
    # concern: if it is absent or malformed the spatial pipeline is unaffected
    # and only the video panel goes dark.
    jpeg_b64 = payload.get("frame_jpeg")
    if isinstance(jpeg_b64, str) and jpeg_b64:
        try:
            frame_buffer.put(
                camera_id=str(payload.get("camera_id", settings.CAMERA_ID)),
                jpeg=base64.b64decode(jpeg_b64),
                frame_id=int(payload.get("frame_id", 0) or 0),
                size=(
                    int(payload.get("frame_width", settings.FRAME_WIDTH) or settings.FRAME_WIDTH),
                    int(payload.get("frame_height", settings.FRAME_HEIGHT) or settings.FRAME_HEIGHT),
                ),
            )
        except (ValueError, TypeError, binascii.Error) as exc:
            log.warning("discarding malformed frame_jpeg: %s", exc)

    names = payload.get("class_names")
    if isinstance(names, dict):
        try:
            names = {int(k): str(v) for k, v in names.items()}
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="class_names keys must be integer class ids",
            ) from None

    frame = vision_adapter.normalize(
        detections,
        class_names=names,
        frame_id=int(payload.get("frame_id", 0) or 0),
        frame_size=(
            int(payload.get("frame_width", settings.FRAME_WIDTH) or settings.FRAME_WIDTH),
            int(payload.get("frame_height", settings.FRAME_HEIGHT) or settings.FRAME_HEIGHT),
        ),
        camera_id=str(payload.get("camera_id", settings.CAMERA_ID)),
    )
    return await fusion_engine.process_frame(frame)


@router.get("/state", summary="Current site state (same payload as the WebSocket)")
async def get_state() -> dict:
    return fusion_engine.site_state()


@router.get("/system/status", summary="Health and diagnostics")
async def system_status() -> dict:
    return {
        "app": settings.APP_NAME,
        "version": settings.VERSION,
        "frames_processed": fusion_engine.frames_processed,
        "fps": fusion_engine.fps,
        "last_frame_at": fusion_engine.last_frame_at,
        "source": "demo" if demo_simulator.running else fusion_engine.last_source,
        "demo_mode": demo_simulator.running,
        "demo_synthetic_calibration": demo_simulator.injected_calibration,
        "calibration": calibration.status(),
        "websocket_clients": manager.client_count,
        "db_queue_dropped": db_writer.dropped,
        "db_records_written": db_writer.written,
        "required_ppe": settings.required_ppe,
    }


# --------------------------------------------------------------------------- #
# Demo mode
# --------------------------------------------------------------------------- #
@router.post("/demo/start", summary="Start demo mode")
async def start_demo() -> dict:
    note = demo_simulator.ensure_calibration()
    await demo_simulator.start()
    return {
        "demo_mode": True,
        "workers": [
            {"track_id": w.track_id, "scenario": w.label} for w in demo_simulator.workers
        ],
        "synthetic_calibration": demo_simulator.injected_calibration,
        "note": note,
    }


@router.post("/demo/stop", summary="Stop demo mode")
async def stop_demo() -> dict:
    await demo_simulator.stop()
    return {"demo_mode": False, "calibration": calibration.status()}


@router.post("/demo/reset", summary="Clear live state (workers, alerts, zone memory)")
async def reset_state() -> dict:
    was_running = demo_simulator.running
    if was_running:
        await demo_simulator.stop()
    fusion_engine.reset()
    if was_running:
        await demo_simulator.start()
    return {"reset": True, "demo_mode": demo_simulator.running}
