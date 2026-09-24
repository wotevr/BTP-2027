"""
Latest-frame buffer for the live camera view.

The dashboard was originally map-only, which turned out to be a real usability
gap: an operator looking at coloured dots on a plan has no way to tell whether
the system is seeing what they think it is seeing. Showing the camera view
beside the map makes the whole pipeline legible - you can watch a bounding box
in pixel space and the corresponding marker in metres at the same time.

Design notes:

* Only the MOST RECENT frame per camera is kept. This is a live view, not a
  recording: a backlog would only add latency, and nothing here is persisted.
* Frames are held as already-encoded JPEG bytes. The bridge encodes once, on
  the machine that already has the decoded image, and the API just relays them.
* Nothing in the safety pipeline depends on this. If frames never arrive, the
  map, the geofencing and the alerts all work exactly as before - the video
  panel simply reports that no feed is connected.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class CameraFrame:
    jpeg: bytes
    frame_id: int
    received_at: float
    width: int
    height: int


@dataclass
class _CameraSlot:
    frame: CameraFrame | None = None
    # Woken on every new frame so streaming clients push immediately rather
    # than polling on a timer.
    event: asyncio.Event = field(default_factory=asyncio.Event)
    frames_received: int = 0


class FrameBuffer:
    def __init__(self, stale_after: float = 10.0) -> None:
        self._cameras: dict[str, _CameraSlot] = {}
        self.stale_after = stale_after

    def _slot(self, camera_id: str) -> _CameraSlot:
        slot = self._cameras.get(camera_id)
        if slot is None:
            slot = _CameraSlot()
            self._cameras[camera_id] = slot
        return slot

    # ------------------------------------------------------------- writing
    def put(self, camera_id: str, jpeg: bytes, frame_id: int, size: tuple[int, int]) -> None:
        slot = self._slot(camera_id)
        slot.frame = CameraFrame(
            jpeg=jpeg,
            frame_id=frame_id,
            received_at=time.monotonic(),
            width=size[0],
            height=size[1],
        )
        slot.frames_received += 1
        # Release anyone waiting, then immediately re-arm for the next frame.
        slot.event.set()
        slot.event.clear()

    # ------------------------------------------------------------- reading
    def latest(self, camera_id: str) -> CameraFrame | None:
        slot = self._cameras.get(camera_id)
        return slot.frame if slot else None

    async def wait_for_frame(self, camera_id: str, timeout: float = 5.0) -> CameraFrame | None:
        """Block until a new frame arrives, or the timeout expires."""
        slot = self._slot(camera_id)
        try:
            await asyncio.wait_for(slot.event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return slot.frame

    # ------------------------------------------------------------- status
    def cameras(self) -> list[dict]:
        now = time.monotonic()
        out = []
        for camera_id, slot in self._cameras.items():
            f = slot.frame
            out.append(
                {
                    "camera_id": camera_id,
                    "has_feed": f is not None and (now - f.received_at) < self.stale_after,
                    "frames_received": slot.frames_received,
                    "last_frame_id": f.frame_id if f else None,
                    "age_seconds": round(now - f.received_at, 2) if f else None,
                    "resolution": [f.width, f.height] if f else None,
                }
            )
        return out

    def is_live(self, camera_id: str) -> bool:
        f = self.latest(camera_id)
        return f is not None and (time.monotonic() - f.received_at) < self.stale_after

    def reset(self) -> None:
        self._cameras.clear()


frame_buffer = FrameBuffer()
