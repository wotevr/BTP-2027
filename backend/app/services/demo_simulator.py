"""
DEMO MODE.

What is simulated and what is not - read this before the viva.

SIMULATED: the detections. Five scripted workers walk along waypoint routes and
the simulator synthesises the bounding boxes a camera would have produced for
them.

NOT SIMULATED: everything after that. The synthetic boxes are emitted in the
EXACT format the upstream YOLO pipeline emits (per-class boxes with track ids),
and are then pushed through the real `vision_adapter`, the real PPE
association, the real homography, the real geofence engine, the real alert
debouncing, the real database writer and the real WebSocket broadcast.

So demo mode proves the spatial safety layer works. It proves nothing about the
detector, and it is labelled DEMO everywhere in the UI for that reason.

HOW THE SYNTHETIC BOXES ARE BUILT
---------------------------------
Each worker has a position in world metres. That position is projected back
into the image with `world_to_pixel` - the inverse of what the fusion engine is
about to do - and a person box is built upward from that foot pixel. The person
height in pixels is derived from the homography itself by measuring how many
pixels 1.7 m spans at that spot, so workers shrink with distance exactly as
they would in a real camera view.

This means the demo actually round-trips through the homography, which is a
genuine test of the calibration rather than a bypass of it.
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.core.config import settings
from app.services.homography import HomographyError, HomographyTransform, calibration
from app.vision.vision_adapter import vision_adapter

log = logging.getLogger(__name__)

PERSON_HEIGHT_M = 1.7
BODY_WIDTH_RATIO = 0.38

# Class ids of the 10-class upstream model (yolo11n.pt).
CLS_HARDHAT = 0
CLS_MASK = 1
CLS_NO_HARDHAT = 2
CLS_NO_MASK = 3
CLS_NO_VEST = 4
CLS_PERSON = 5
CLS_VEST = 7

DEMO_CLASS_NAMES = {
    0: "Hardhat", 1: "Mask", 2: "NO-Hardhat", 3: "NO-Mask", 4: "NO-Safety Vest",
    5: "Person", 6: "Safety Cone", 7: "Safety Vest", 8: "machinery", 9: "vehicle",
}


def demo_calibration_points() -> tuple[list[list[float]], list[list[float]]]:
    """
    A plausible fixed camera looking across a 30 m x 20 m site from the Y=0 edge.

    The image points form a trapezoid: the far edge of the site (Y = 20 m)
    appears narrower and higher in the frame than the near edge, which is what
    perspective does.
    """
    image_points = [
        [60.0, 450.0],    # near-left
        [580.0, 450.0],   # near-right
        [430.0, 150.0],   # far-right
        [210.0, 150.0],   # far-left
    ]
    world_points = [
        [0.0, 0.0],
        [30.0, 0.0],
        [30.0, 20.0],
        [0.0, 20.0],
    ]
    return image_points, world_points


def build_demo_transform() -> HomographyTransform:
    img, wld = demo_calibration_points()
    return HomographyTransform.from_points(
        img, wld, image_size=(settings.FRAME_WIDTH, settings.FRAME_HEIGHT)
    )


# --------------------------------------------------------------------------- #
# Scripted workers
# --------------------------------------------------------------------------- #
@dataclass
class DemoWorker:
    track_id: int
    label: str
    route: list[tuple[float, float]]
    speed: float = 1.3                 # metres per second, a normal walk
    helmet: bool | None = True
    vest: bool | None = True
    mask: bool | None = None           # None = detector reports nothing
    confidence: float = 0.93
    pause_at_waypoint: float = 0.0

    _leg: int = field(default=0, init=False)
    _t: float = field(default=0.0, init=False)
    _paused_until: float = field(default=0.0, init=False)

    def position(self) -> tuple[float, float]:
        a = self.route[self._leg % len(self.route)]
        b = self.route[(self._leg + 1) % len(self.route)]
        leg_len = math.dist(a, b)
        if leg_len <= 0:
            return a
        f = min(1.0, self._t / leg_len)
        return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)

    def advance(self, dt: float, now: float) -> None:
        if now < self._paused_until:
            return
        a = self.route[self._leg % len(self.route)]
        b = self.route[(self._leg + 1) % len(self.route)]
        leg_len = math.dist(a, b)
        self._t += self.speed * dt
        if self._t >= leg_len:
            self._t = 0.0
            self._leg = (self._leg + 1) % len(self.route)
            if self.pause_at_waypoint:
                self._paused_until = now + self.pause_at_waypoint


def default_demo_workers() -> list[DemoWorker]:
    """
    The five scenarios from the project brief. Routes are chosen to line up with
    the danger zones created by `scripts/seed_demo_data.py`.
    """
    return [
        # 1. Normal worker, full PPE. The route is deliberately kept clear of
        #    every zone polygon so this worker stays SAFE for the whole demo.
        DemoWorker(
            track_id=1,
            label="Normal patrol, full PPE, stays clear of all zones",
            route=[(3.0, 3.5), (18.0, 3.5), (18.0, 8.0), (3.0, 8.0)],
            helmet=True, vest=True, mask=True, speed=1.4,
        ),
        # 2. Walks into the heavy machinery zone, waits there, walks out. The
        #    route stays clear of the restricted store so this worker only ever
        #    triggers the machinery breach.
        DemoWorker(
            track_id=2,
            label="Enters Heavy Machinery Zone",
            route=[(8.0, 16.0), (14.0, 12.5), (18.0, 12.5), (18.0, 17.5), (8.0, 17.5)],
            helmet=True, vest=True, speed=1.1, pause_at_waypoint=3.0,
        ),
        # 3. Missing hardhat the whole time, but never enters a zone - so the
        #    only thing raised for this worker is the PPE violation.
        DemoWorker(
            track_id=3,
            label="No hardhat, no zone breach",
            route=[(22.5, 9.5), (28.0, 9.5), (28.0, 13.5), (22.5, 13.5)],
            helmet=False, vest=True, speed=1.0,
        ),
        # 4. Enters the restricted materials store and lingers there, so the
        #    breach is comfortably visible during a live demonstration.
        DemoWorker(
            track_id=4,
            label="Restricted area breach",
            route=[(2.0, 11.0), (3.0, 16.0), (5.0, 18.5), (2.5, 18.0), (1.8, 12.5)],
            helmet=True, vest=True, speed=0.8, pause_at_waypoint=4.0,
        ),
        # 5. Crosses excavation and open edge, and is missing a vest.
        DemoWorker(
            track_id=5,
            label="Multi-zone crossing, no vest",
            route=[(10.0, 1.5), (24.0, 2.5), (28.5, 10.0), (24.0, 18.5), (10.0, 18.0)],
            helmet=True, vest=False, speed=1.5,
        ),
    ]


# --------------------------------------------------------------------------- #
# Box synthesis
# --------------------------------------------------------------------------- #
def _pixels_per_metre(transform: HomographyTransform, x: float, y: float) -> float:
    """
    Vertical image scale at a world point, measured from the homography.

    Projects the point and a point 1 m nearer the camera, and returns the pixel
    distance between them. Near the camera this is large, far away it is small -
    which is exactly the perspective foreshortening we want the boxes to have.
    """
    u0, v0 = transform.world_to_pixel(x, y)
    u1, v1 = transform.world_to_pixel(x, max(0.0, y - 1.0))
    d = math.dist((u0, v0), (u1, v1))
    return d if d > 1e-3 else 20.0


def synth_detections(
    workers: list[DemoWorker], transform: HomographyTransform
) -> list[dict]:
    """Build upstream-format detections for the current worker positions."""
    detections: list[dict] = []

    for w in workers:
        x, y = w.position()
        try:
            u, v = transform.world_to_pixel(x, y)
        except HomographyError:
            continue

        ppm = _pixels_per_metre(transform, x, y)
        h = max(24.0, PERSON_HEIGHT_M * ppm)
        bw = max(10.0, h * BODY_WIDTH_RATIO)

        # (u, v) is the FOOT point, so the box grows upward from it.
        x1, y1, x2, y2 = u - bw / 2.0, v - h, u + bw / 2.0, v
        detections.append(
            {
                "bbox": [x1, y1, x2, y2],
                "conf": w.confidence,
                "class_id": CLS_PERSON,
                "track_id": w.track_id,
            }
        )

        head_h = h * 0.16
        # Hardhat sits on top of the head.
        if w.helmet is not None:
            detections.append(
                {
                    "bbox": [u - bw * 0.30, y1 + head_h * 0.10, u + bw * 0.30, y1 + head_h],
                    "conf": 0.88 if w.helmet else 0.81,
                    "class_id": CLS_HARDHAT if w.helmet else CLS_NO_HARDHAT,
                    "track_id": None,
                }
            )
        # Vest covers the torso.
        if w.vest is not None:
            detections.append(
                {
                    "bbox": [u - bw * 0.45, y1 + h * 0.22, u + bw * 0.45, y1 + h * 0.58],
                    "conf": 0.86 if w.vest else 0.79,
                    "class_id": CLS_VEST if w.vest else CLS_NO_VEST,
                    "track_id": None,
                }
            )
        # Mask over the lower face.
        if w.mask is not None:
            detections.append(
                {
                    "bbox": [u - bw * 0.18, y1 + head_h * 0.9, u + bw * 0.18, y1 + head_h * 1.9],
                    "conf": 0.74 if w.mask else 0.70,
                    "class_id": CLS_MASK if w.mask else CLS_NO_MASK,
                    "track_id": None,
                }
            )

    return detections


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
class DemoSimulator:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False
        self.workers: list[DemoWorker] = []
        self.frame_id = 0
        self.injected_calibration = False

    @property
    def running(self) -> bool:
        return self._running

    def ensure_calibration(self) -> str | None:
        """
        Demo mode needs a homography to project workers into the image.

        If the operator has calibrated a real camera we use that. Otherwise we
        install the synthetic demo camera described above, in memory only - the
        file on disk is never overwritten - and flag it so the UI can say so.
        """
        if calibration.available:
            return None
        calibration.transform = build_demo_transform()
        calibration.error = None
        self.injected_calibration = True
        msg = (
            "No camera calibration found - demo mode installed a synthetic demo "
            "camera (30m x 20m site). Real coordinates need a real calibration."
        )
        log.warning(msg)
        return msg

    async def start(self, workers: list[DemoWorker] | None = None) -> None:
        from app.services.fusion_engine import fusion_engine

        if self._running:
            return
        self.workers = workers or default_demo_workers()
        self.frame_id = 0
        self.ensure_calibration()
        self._running = True
        fusion_engine.demo_active = True
        self._task = asyncio.create_task(self._run(), name="demo-simulator")
        log.info("Demo mode started with %d simulated workers", len(self.workers))

    async def stop(self) -> None:
        from app.services.fusion_engine import fusion_engine

        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        fusion_engine.demo_active = False
        if self.injected_calibration:
            calibration.transform = None
            calibration.load(settings.homography_path)
            self.injected_calibration = False
        log.info("Demo mode stopped")

    async def _run(self) -> None:
        from app.services.fusion_engine import fusion_engine

        dt = 1.0 / max(settings.DEMO_FPS, 1.0)
        loop = asyncio.get_running_loop()
        while self._running:
            started = loop.time()
            try:
                transform = calibration.transform
                if transform is None:
                    await asyncio.sleep(1.0)
                    continue

                for w in self.workers:
                    w.advance(dt, started)

                raw = synth_detections(self.workers, transform)
                self.frame_id += 1

                # The same adapter the real pipeline uses.
                frame = vision_adapter.normalize(
                    raw,
                    class_names=DEMO_CLASS_NAMES,
                    frame_id=self.frame_id,
                    timestamp=datetime.now(timezone.utc),
                    frame_size=(settings.FRAME_WIDTH, settings.FRAME_HEIGHT),
                    camera_id="DEMO-CAM",
                    source="demo",
                )
                await fusion_engine.process_frame(frame)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad frame must not kill the demo
                log.exception("Demo frame failed")

            elapsed = loop.time() - started
            await asyncio.sleep(max(0.0, dt - elapsed))


demo_simulator = DemoSimulator()
