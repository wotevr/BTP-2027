"""
THE LIVE BRIDGE: upstream YOLO pipeline  ->  BTP-2027 spatial safety layer.

This script is deliberately STANDALONE. It does not import anything from the
BTP-2027 `app` package, for two reasons:

  1. Both repositories have a top-level package called `app`. Importing both
     into one interpreter would collide.
  2. Only this script needs torch / ultralytics. Keeping it separate means the
     BTP-2027 API stays a light process with no GPU dependencies at all.

So it runs in the UPSTREAM environment (the one that already has torch,
ultralytics and the trained weights), reuses the existing YOLODetector exactly
as it is, and POSTs the detections it returns to the BTP-2027 API.

    CCTV / video
        |
        v
    upstream YOLODetector.detect()        <- existing code, existing weights,
        |                                    existing BoT-SORT tracker
        v
    POST /api/v1/detections/raw           <- this script
        |
        v
    vision adapter -> foot point -> homography -> geofence -> alerts

NOTHING is re-detected and NOTHING is re-tracked. The persistent track ids that
BoT-SORT assigns are passed straight through and become the worker ids.

USAGE
-----
From the upstream repository environment:

    python yolo_bridge.py --upstream /path/to/Construction_Safety_BTP/Backend \
                          --source 0 \
                          --api http://localhost:8000

    --source accepts a webcam index (0), a video file, or an rtsp:// URL.

If the upstream repository is not importable but you do have its weights, the
--weights flag runs ultralytics directly with the SAME model and the SAME
tracker configuration the upstream code uses:

    python yolo_bridge.py --weights /path/to/yolo11n.pt --source site.mp4

Check it is working without a camera:

    python yolo_bridge.py --self-test
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("ERROR: `pip install requests` is needed to run the bridge.")


# --------------------------------------------------------------------------- #
# Detector loading
# --------------------------------------------------------------------------- #
def load_upstream_detector(upstream_backend: str, weights: str | None):
    """
    Import the EXISTING YOLODetector from Construction_Safety_BTP.

    `upstream_backend` is the directory that contains the `app/` package, i.e.
    .../Construction_Safety_BTP/Backend
    """
    root = Path(upstream_backend).expanduser().resolve()
    models = root / "app" / "models"
    if not (models / "yolo_detector.py").exists():
        sys.exit(
            f"ERROR: {root} does not look like the upstream Backend directory.\n"
            "Expected to find app/models/yolo_detector.py under it."
        )

    # Load yolo_detector.py directly instead of `from app.models...`.
    #
    # The upstream app/models/__init__.py instantiates the whole SafetyMonitor
    # at import time, which pulls in MediaPipe and loads the model just to
    # satisfy an import. Pose estimation and RULA/REBA are a separate concern
    # from spatial tracking, so we bypass that package __init__ and execute
    # yolo_detector.py on its own - unchanged, relative `from . import
    # torch_patch` included - by mapping a synthetic package onto its folder.
    sys.path.insert(0, str(root))
    try:
        pkg_name = "_upstream_models"
        if pkg_name not in sys.modules:
            pkg = types.ModuleType(pkg_name)
            pkg.__path__ = [str(models)]
            sys.modules[pkg_name] = pkg
        spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.yolo_detector", models / "yolo_detector.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{pkg_name}.yolo_detector"] = module
        spec.loader.exec_module(module)
        YOLODetector = module.YOLODetector
    except ImportError as exc:
        sys.exit(
            f"ERROR: could not import the upstream detector: {exc}\n"
            "Run this script in the environment that has torch + ultralytics "
            "installed (the upstream backend environment)."
        )

    model_path = weights or str(root / "yolo_models" / "yolo11n.pt")
    if not Path(model_path).exists():
        sys.exit(f"ERROR: weights not found: {model_path}")

    print(f"Loading upstream YOLODetector with {model_path}")
    detector = YOLODetector(model_path)
    names = getattr(detector.model, "names", None)
    return detector.detect, dict(names) if names else None


def load_ultralytics_detector(weights: str):
    """
    Fallback: drive ultralytics directly, with the SAME settings the upstream
    YOLODetector uses (BoT-SORT, persist=True, conf=0.1), so behaviour matches.
    """
    try:
        from ultralytics import YOLO  # type: ignore
    except ImportError:
        sys.exit("ERROR: ultralytics is not installed in this environment.")

    if not Path(weights).exists():
        sys.exit(f"ERROR: weights not found: {weights}")

    print(f"Loading ultralytics YOLO with {weights} (BoT-SORT, persist=True)")
    model = YOLO(weights)

    def detect(frame):
        results = model.track(
            frame, persist=True, tracker="botsort.yaml", verbose=False, conf=0.1
        )
        out = []
        for det in results[0].boxes:
            x1, y1, x2, y2 = det.xyxy[0].cpu().numpy()
            out.append(
                {
                    "bbox": [int(x1), int(y1), int(x2), int(y2)],
                    "conf": float(det.conf[0]),
                    "class_id": int(det.cls[0]),
                    "track_id": int(det.id[0]) if det.id is not None else None,
                }
            )
        return out

    return detect, dict(model.names)


# --------------------------------------------------------------------------- #
# Posting
# --------------------------------------------------------------------------- #
class Poster:
    def __init__(self, api: str, camera_id: str, timeout: float = 3.0) -> None:
        self.url = api.rstrip("/") + "/api/v1/detections/raw"
        self.camera_id = camera_id
        self.timeout = timeout
        self.session = requests.Session()
        self.sent = 0
        self.failed = 0
        self.alerts = 0
        self._warned = False

    def send(self, detections, class_names, frame_id, size) -> dict | None:
        payload = {
            "camera_id": self.camera_id,
            "frame_id": frame_id,
            "frame_width": size[0],
            "frame_height": size[1],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "detections": detections,
        }
        if class_names:
            payload["class_names"] = {str(k): v for k, v in class_names.items()}
        try:
            r = self.session.post(self.url, json=payload, timeout=self.timeout)
            r.raise_for_status()
            self.sent += 1
            self._warned = False
            data = r.json()
            self.alerts += int(data.get("alerts_generated", 0))
            return data
        except requests.RequestException as exc:
            self.failed += 1
            # Do not spam: one message per outage, not one per frame.
            if not self._warned:
                print(f"  ! cannot reach {self.url}: {exc}")
                print("    (retrying every frame, will report when it recovers)")
                self._warned = True
            return None


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
def run(args) -> int:
    import cv2

    if args.weights and not args.upstream:
        detect, class_names = load_ultralytics_detector(args.weights)
    else:
        detect, class_names = load_upstream_detector(args.upstream, args.weights)

    if class_names:
        print(f"Model classes ({len(class_names)}): {class_names}")
    else:
        print("WARNING: model class names unavailable; the API will fall back to")
        print("         its default table, which assumes Person is class 5.")

    source: str | int = args.source
    if isinstance(source, str) and source.isdigit():
        source = int(source)

    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        print(f"ERROR: cannot open video source {source!r}")
        return 2

    poster = Poster(args.api, args.camera_id)
    size = (args.width, args.height)
    print(f"Streaming {source!r} -> {poster.url}")
    print(f"Resizing frames to {size[0]}x{size[1]} to match the upstream pipeline")
    print("Ctrl-C to stop.\n")

    frame_id = 0
    last_report = time.time()
    frames_since_report = 0
    min_interval = 1.0 / args.fps if args.fps > 0 else 0.0

    try:
        while True:
            started = time.time()
            ok, frame = cap.read()
            if not ok:
                if args.loop and not isinstance(source, int):
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                print("End of stream.")
                break

            # The upstream pipeline resizes to 640x480 before inference. We do
            # exactly the same so the boxes land in the pixel space the
            # calibration was measured in.
            frame = cv2.resize(frame, size)
            frame_id += 1

            try:
                detections = detect(frame)
            except Exception as exc:  # noqa: BLE001 - keep streaming on a bad frame
                print(f"  ! detection failed on frame {frame_id}: {exc}")
                continue

            result = poster.send(detections, class_names, frame_id, size)
            frames_since_report += 1

            if result and result.get("alerts_generated"):
                print(
                    f"  frame {frame_id}: {result['processed_workers']} worker(s), "
                    f"{result['alerts_generated']} new alert(s)"
                )
            if result and not result.get("spatial_calibration"):
                if frame_id % 100 == 1:
                    print(f"  note: {result.get('detail')}")

            now = time.time()
            if now - last_report >= 5.0:
                fps = frames_since_report / (now - last_report)
                print(
                    f"  [{frame_id:>6}] {fps:4.1f} fps  sent={poster.sent} "
                    f"failed={poster.failed} alerts={poster.alerts}"
                )
                last_report, frames_since_report = now, 0

            if min_interval:
                time.sleep(max(0.0, min_interval - (time.time() - started)))
    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        cap.release()

    print(f"\nFrames sent: {poster.sent}, failed: {poster.failed}, alerts: {poster.alerts}")
    return 0


def self_test(api: str, camera_id: str) -> int:
    """
    Post one hand-written frame in the upstream format. Verifies the API is up
    and the adapter is wired correctly, with no camera and no model.
    """
    print(f"Self-test against {api}")
    detections = [
        # A person, tracked as id 17, standing in the lower middle of the frame.
        {"bbox": [300, 240, 345, 380], "conf": 0.94, "class_id": 5, "track_id": 17},
        # A hardhat on their head.
        {"bbox": [310, 243, 336, 258], "conf": 0.88, "class_id": 0, "track_id": None},
        # And NO safety vest on their torso.
        {"bbox": [303, 272, 342, 320], "conf": 0.81, "class_id": 4, "track_id": None},
    ]
    class_names = {
        0: "Hardhat", 1: "Mask", 2: "NO-Hardhat", 3: "NO-Mask", 4: "NO-Safety Vest",
        5: "Person", 6: "Safety Cone", 7: "Safety Vest", 8: "machinery", 9: "vehicle",
    }
    poster = Poster(api, camera_id)
    result = poster.send(detections, class_names, frame_id=1, size=(640, 480))
    if result is None:
        print("FAILED: the API did not respond. Is the backend running?")
        return 1
    print(f"  response: {result}")
    if result.get("processed_workers") != 1:
        print("FAILED: expected exactly 1 processed worker.")
        return 1
    print("\nOK - the API accepted the frame, associated the PPE boxes to")
    print("track 17 and ran it through the spatial pipeline.")
    print("Open the dashboard: worker #17 should be on the map.")
    if not result.get("spatial_calibration"):
        print(f"\nNote: {result.get('detail')}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Stream detections from the upstream YOLO pipeline into BTP-2027.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--upstream", help="path to Construction_Safety_BTP/Backend")
    p.add_argument("--weights", help="path to the .pt weights")
    p.add_argument("--source", default="0", help="webcam index, video file or rtsp:// URL")
    p.add_argument("--api", default="http://localhost:8000", help="BTP-2027 API base URL")
    p.add_argument("--camera-id", default="camera_01")
    p.add_argument("--width", type=int, default=640, help="inference width (must match calibration)")
    p.add_argument("--height", type=int, default=480, help="inference height")
    p.add_argument("--fps", type=float, default=10.0, help="cap the send rate (0 = uncapped)")
    p.add_argument("--loop", action="store_true", help="restart a video file when it ends")
    p.add_argument("--self-test", action="store_true", help="post one synthetic frame and exit")
    args = p.parse_args()

    if args.self_test:
        return self_test(args.api, args.camera_id)
    if not args.upstream and not args.weights:
        p.error("give --upstream (preferred) or --weights, or use --self-test")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
