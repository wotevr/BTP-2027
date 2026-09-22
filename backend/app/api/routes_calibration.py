"""
Calibration inspection and hot reload.

Calibration is normally produced offline by `scripts/calibrate_homography.py`.
These endpoints let the dashboard show the current state, verify a point, and
pick up a new file without restarting the server.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from app.core.config import settings
from app.services.homography import HomographyError, HomographyTransform, calibration

router = APIRouter()


@router.get("/calibration", summary="Current spatial calibration")
def get_calibration() -> dict:
    payload = calibration.status()
    if calibration.transform is not None:
        payload["homography"] = calibration.transform.matrix.tolist()
        payload["image_points"] = calibration.transform.image_points
        payload["world_points"] = calibration.transform.world_points
    payload["site"] = {"width_m": settings.SITE_WIDTH_M, "depth_m": settings.SITE_DEPTH_M}
    return payload


@router.post("/calibration/reload", summary="Re-read the homography file from disk")
def reload_calibration() -> dict:
    calibration.load(settings.homography_path)
    return calibration.status()


@router.post("/calibration/preview", summary="Compute a homography without saving it")
def preview_calibration(payload: dict = Body(...)) -> dict:
    """
    Validate a candidate set of correspondences and report the reprojection
    error, so the calibration can be checked before it is written to disk.
    """
    try:
        size = payload.get("image_size")
        transform = HomographyTransform.from_points(
            payload.get("image_points"),
            payload.get("world_points"),
            tuple(size) if size else (settings.FRAME_WIDTH, settings.FRAME_HEIGHT),
        )
    except HomographyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return transform.to_dict()


@router.post("/calibration/project", summary="Convert a pixel to site metres")
def project_point(payload: dict = Body(...)) -> dict:
    """
    Debug helper. Give it a pixel and it returns the world point, or give it a
    world point and it returns the pixel.
    """
    if calibration.transform is None:
        raise HTTPException(status_code=409, detail=calibration.error or "no calibration loaded")
    try:
        if "pixel" in payload:
            u, v = payload["pixel"]
            x, y = calibration.transform.pixel_to_world(float(u), float(v))
            return {"pixel": [u, v], "world": [round(x, 3), round(y, 3)]}
        if "world" in payload:
            x, y = payload["world"]
            u, v = calibration.transform.world_to_pixel(float(x), float(y))
            return {"world": [x, y], "pixel": [round(u, 2), round(v, 2)]}
    except (HomographyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise HTTPException(status_code=422, detail="send either 'pixel': [u, v] or 'world': [x, y]")
