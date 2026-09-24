"""
Live camera view.

Serves whatever frames the bridge has pushed, as MJPEG. An <img> tag pointed at
the stream endpoint renders continuous video with no JavaScript, no WebSocket
and no media library - which is the right amount of machinery for a wall
display in a site office.

This is a view onto the pipeline, not part of it. No safety decision is made
here; the frames are already annotated upstream by the detector that produced
the boxes.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, StreamingResponse

from app.services.frame_buffer import frame_buffer

log = logging.getLogger(__name__)
router = APIRouter()

BOUNDARY = "btpframe"


@router.get("/camera/status", summary="Which cameras are pushing frames")
def camera_status() -> dict:
    cams = frame_buffer.cameras()
    return {"cameras": cams, "any_live": any(c["has_feed"] for c in cams)}


@router.get("/camera/{camera_id}/snapshot.jpg", summary="Most recent frame")
def snapshot(camera_id: str) -> Response:
    frame = frame_buffer.latest(camera_id)
    if frame is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no frames received for camera '{camera_id}'. Start the bridge "
                "with --send-frames to feed the live view."
            ),
        )
    return Response(
        content=frame.jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/camera/{camera_id}/stream.mjpg", summary="Live MJPEG stream")
async def stream(camera_id: str, fps: float = Query(12.0, gt=0, le=30)) -> StreamingResponse:
    """
    multipart/x-mixed-replace, the format every browser renders natively in an
    <img>. Frames are pushed as they arrive rather than polled, so the stream
    runs at whatever rate the upstream pipeline actually manages, capped by
    `fps` so a fast source cannot saturate the connection.
    """
    min_gap = 1.0 / fps

    async def frames():
        last_id = -1
        idle = 0.0
        while True:
            frame = frame_buffer.latest(camera_id)

            if frame is not None and frame.frame_id != last_id:
                last_id = frame.frame_id
                idle = 0.0
                yield (
                    b"--" + BOUNDARY.encode() + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(frame.jpeg)).encode() + b"\r\n\r\n"
                    + frame.jpeg + b"\r\n"
                )
                await asyncio.sleep(min_gap)
                continue

            # Nothing new: wait to be woken rather than spinning.
            got = await frame_buffer.wait_for_frame(camera_id, timeout=2.0)
            if got is None:
                idle += 2.0
                # Give up on a feed that has gone quiet, so the connection does
                # not linger forever after the bridge stops.
                if idle > 60.0:
                    log.info("Closing stale MJPEG stream for %s", camera_id)
                    return

    return StreamingResponse(
        frames(),
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
        headers={"Cache-Control": "no-store", "Connection": "close"},
    )
