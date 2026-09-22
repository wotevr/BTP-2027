"""
Homography calibration tool.

WHAT YOU ARE DOING
------------------
You are telling the system how pixels in the camera image correspond to metres
on the ground. You need at least FOUR points that

  * lie on the ground plane (floor level, not on top of a wall or a container),
  * you can see clearly in the camera image, and
  * you can measure the real position of, in metres, from a chosen origin.

Good candidates: corners of a rectangular slab, painted parking bays, the four
base corners of a fenced compound, survey pegs, or four cones you place
yourself and then measure with a tape.

Pick an origin (say the south-west corner of the site) and one axis direction
(say X runs east along the site width, Y runs north along the depth). Every
world measurement is in metres from that origin. This is a LOCAL SITE frame -
it is not GPS and it does not need to be.

THREE WAYS TO RUN IT
--------------------
1. Click on the points (needs a frame image or a video):

       python scripts/calibrate_homography.py --image frame.jpg
       python scripts/calibrate_homography.py --video ../video/site.mp4

   Click each ground point in the window, then type its world X and Y in the
   terminal. Press `u` to undo the last point, `q` when you are done.

2. Type everything in the terminal, no image needed:

       python scripts/calibrate_homography.py --interactive

3. Load the correspondences from a JSON file:

       python scripts/calibrate_homography.py --from-json points.json

   where points.json is {"image_points": [[u,v],...], "world_points": [[X,Y],...]}

The result is written to data/calibration/homography.json and picked up on the
next backend start (or immediately via POST /api/v1/calibration/reload).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running as `python scripts/calibrate_homography.py` from backend/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.services.homography import (  # noqa: E402
    MIN_POINTS,
    HomographyError,
    HomographyTransform,
)

WINDOW = "Calibration - click ground points, u = undo, q = done"


# --------------------------------------------------------------------------- #
# Input helpers
# --------------------------------------------------------------------------- #
def ask_float(prompt: str) -> float:
    while True:
        raw = input(prompt).strip()
        try:
            return float(raw)
        except ValueError:
            print(f"  '{raw}' is not a number. Try again, for example 12.5")


def ask_world_point(index: int) -> list[float]:
    print(f"\n  Point {index + 1} - real-world position, in metres from your origin:")
    x = ask_float("    X (along site width)  = ")
    y = ask_float("    Y (along site depth)  = ")
    return [x, y]


def grab_frame(image: str | None, video: str | None) -> np.ndarray:
    if image:
        path = Path(image)
        if not path.exists():
            raise SystemExit(f"ERROR: image not found: {path}")
        frame = cv2.imread(str(path))
        if frame is None:
            raise SystemExit(f"ERROR: could not decode image: {path}")
        return frame

    source: str | int = video or settings.VIDEO_SOURCE
    if isinstance(source, str) and source.isdigit():
        source = int(source)
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        raise SystemExit(
            f"ERROR: could not open video source {source!r}.\n"
            "Pass --image with a saved frame instead, or use --interactive."
        )
    ok, frame = cap.read()
    cap.release()
    if not ok or frame is None:
        raise SystemExit(f"ERROR: could not read a frame from {source!r}")
    return frame


# --------------------------------------------------------------------------- #
# Collection modes
# --------------------------------------------------------------------------- #
def collect_by_clicking(frame: np.ndarray) -> tuple[list, list, tuple[int, int]]:
    """
    Click points on the image; type the world coordinates in the terminal.

    The frame is resized to the pixel space the upstream pipeline actually
    produces boxes in (640x480 by default), so the calibration is expressed in
    the same units as the detections it will be applied to.
    """
    target = (settings.FRAME_WIDTH, settings.FRAME_HEIGHT)
    if (frame.shape[1], frame.shape[0]) != target:
        print(
            f"Resizing frame {frame.shape[1]}x{frame.shape[0]} -> "
            f"{target[0]}x{target[1]} to match the detection pixel space."
        )
        frame = cv2.resize(frame, target)

    image_points: list[list[float]] = []
    world_points: list[list[float]] = []
    pending: list[tuple[int, int]] = []

    def on_mouse(event, x, y, _flags, _param):
        if event == cv2.EVENT_LBUTTONDOWN:
            pending.append((x, y))

    try:
        cv2.namedWindow(WINDOW)
    except cv2.error as exc:
        raise SystemExit(
            "ERROR: this OpenCV build has no GUI support, so points cannot be\n"
            f"clicked ({exc}).\n\n"
            "Either install the full build:\n"
            "    pip uninstall -y opencv-python-headless && pip install opencv-python\n"
            "or enter the coordinates by hand:\n"
            "    python scripts/calibrate_homography.py --interactive"
        ) from exc
    cv2.setMouseCallback(WINDOW, on_mouse)

    print("\n" + "=" * 66)
    print("Click a ground point in the window, then type its world X and Y here.")
    print(f"You need at least {MIN_POINTS} points. Press 'u' to undo, 'q' when done.")
    print("=" * 66)

    while True:
        canvas = frame.copy()
        for i, (px, py) in enumerate(image_points):
            p = (int(px), int(py))
            cv2.circle(canvas, p, 6, (0, 220, 0), -1)
            cv2.circle(canvas, p, 10, (255, 255, 255), 1)
            wx, wy = world_points[i]
            cv2.putText(
                canvas, f"{i+1}: ({wx:g}, {wy:g})m", (p[0] + 12, p[1] - 8),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1, cv2.LINE_AA,
            )
        if len(image_points) >= 2:
            cv2.polylines(
                canvas, [np.int32(image_points)], len(image_points) >= 3,
                (0, 180, 255), 1, cv2.LINE_AA,
            )
        cv2.putText(
            canvas, f"{len(image_points)} point(s) - need {MIN_POINTS}",
            (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA,
        )
        cv2.imshow(WINDOW, canvas)

        key = cv2.waitKey(30) & 0xFF
        if key == ord("q"):
            if len(image_points) < MIN_POINTS:
                print(f"  Need at least {MIN_POINTS} points, have {len(image_points)}.")
                continue
            break
        if key == ord("u") and image_points:
            image_points.pop()
            world_points.pop()
            print(f"  Undone. {len(image_points)} point(s) left.")

        while pending:
            px, py = pending.pop(0)
            print(f"\n  Clicked pixel ({px}, {py})")
            world_points.append(ask_world_point(len(image_points)))
            image_points.append([float(px), float(py)])

    cv2.destroyAllWindows()
    return image_points, world_points, target


def collect_interactively() -> tuple[list, list, tuple[int, int]]:
    print("\n" + "=" * 66)
    print("Interactive calibration - no image needed.")
    print("For each correspondence, give the PIXEL position in the camera image")
    print("and the WORLD position in metres from your site origin.")
    print("=" * 66)

    n = 0
    while n < MIN_POINTS:
        raw = input(f"\nHow many points? (minimum {MIN_POINTS}): ").strip()
        try:
            n = int(raw)
        except ValueError:
            n = 0
        if n < MIN_POINTS:
            print(f"  A homography needs at least {MIN_POINTS} correspondences.")

    image_points, world_points = [], []
    for i in range(n):
        print(f"\n--- Correspondence {i + 1} of {n} ---")
        print("  Image pixel:")
        u = ask_float("    u (pixels from left) = ")
        v = ask_float("    v (pixels from top)  = ")
        image_points.append([u, v])
        world_points.append(ask_world_point(i))

    print(f"\nPixel space of these coordinates [{settings.FRAME_WIDTH}x{settings.FRAME_HEIGHT}]:")
    w = input(f"  width  (Enter for {settings.FRAME_WIDTH}): ").strip()
    h = input(f"  height (Enter for {settings.FRAME_HEIGHT}): ").strip()
    size = (
        int(w) if w.isdigit() else settings.FRAME_WIDTH,
        int(h) if h.isdigit() else settings.FRAME_HEIGHT,
    )
    return image_points, world_points, size


def collect_from_json(path: str) -> tuple[list, list, tuple[int, int]]:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"ERROR: file not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ERROR: {p} is not valid JSON: {exc}") from exc

    for key in ("image_points", "world_points"):
        if key not in data:
            raise SystemExit(f"ERROR: {p} is missing '{key}'")
    size = data.get("image_size") or [settings.FRAME_WIDTH, settings.FRAME_HEIGHT]
    return data["image_points"], data["world_points"], (int(size[0]), int(size[1]))


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def report(transform: HomographyTransform) -> None:
    print("\n" + "=" * 66)
    print("HOMOGRAPHY (world metres -> image pixels)")
    print("=" * 66)
    for row in transform.matrix:
        print("   [ " + "  ".join(f"{v: 12.6f}" for v in row) + " ]")

    err = transform.reprojection_error()
    if err is not None:
        print(f"\nMean reprojection error: {err:.2f} px")
        if err < 3:
            print("  Excellent - the correspondences are consistent.")
        elif err < 10:
            print("  Acceptable. Re-measuring the worst point would improve it.")
        else:
            print(
                "  HIGH. Usually means a point was mis-clicked, two points were\n"
                "  swapped, or a point is not actually on the ground plane."
            )

    print("\nSanity check - your calibration points round-tripped:")
    print("   world (m)          -> pixel        -> world (m)")
    for (wx, wy), (px, py) in zip(transform.world_points, transform.image_points):
        try:
            bx, by = transform.pixel_to_world(px, py)
            print(f"   ({wx:7.2f},{wy:7.2f})  -> ({px:6.1f},{py:6.1f}) -> ({bx:7.2f},{by:7.2f})")
        except HomographyError as exc:
            print(f"   ({wx:7.2f},{wy:7.2f})  -> FAILED: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Calibrate the pixel-to-metres homography for one camera.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--image", help="calibrate by clicking on this image file")
    src.add_argument("--video", help="grab the first frame of this video/RTSP/webcam index")
    src.add_argument("--interactive", action="store_true", help="type coordinates, no image")
    src.add_argument("--from-json", help="read image_points/world_points from a JSON file")
    parser.add_argument(
        "--output", default=None, help=f"output file (default: {settings.HOMOGRAPHY_FILE})"
    )
    parser.add_argument("--force", action="store_true", help="overwrite without asking")
    args = parser.parse_args()

    try:
        if args.from_json:
            image_points, world_points, size = collect_from_json(args.from_json)
        elif args.interactive:
            image_points, world_points, size = collect_interactively()
        else:
            image_points, world_points, size = collect_by_clicking(
                grab_frame(args.image, args.video)
            )
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 1

    try:
        transform = HomographyTransform.from_points(image_points, world_points, size)
    except HomographyError as exc:
        print(f"\nCALIBRATION FAILED\n  {exc}\n")
        print(
            "Common causes:\n"
            "  * fewer than 4 points\n"
            "  * three or more points on a straight line (they must form a\n"
            "    proper quadrilateral, e.g. the corners of a rectangle)\n"
            "  * image point i and world point i are not the same physical spot\n"
            "  * a point is not on the ground plane\n"
        )
        return 2

    report(transform)

    out = Path(args.output) if args.output else settings.homography_path
    if out.exists() and not args.force:
        answer = input(f"\n{out} exists. Overwrite? [y/N] ").strip().lower()
        if answer != "y":
            print("Not saved.")
            return 0

    saved = transform.save(out)
    print(f"\nSaved to {saved}")
    print("Reload it without restarting:  POST /api/v1/calibration/reload")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
