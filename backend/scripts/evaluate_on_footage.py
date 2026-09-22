"""
Quantitative evaluation of the BTP-2027 spatial layer on REAL footage.

Everything here is measured on the senior's own test.mp4 with his own weights.
No synthetic data. These are the numbers for the results section of the report.

Measured:
  1. Detection yield      - persons per frame, PPE boxes per frame
  2. Track fragmentation  - how many distinct track_ids BoT-SORT issues, and
                            how long each survives. This bounds how well
                            track_id works as a worker identity.
  3. PPE association      - coverage (what fraction of person-frames received a
                            helmet/vest decision) and contention (how often one
                            PPE box is plausibly claimable by more than one
                            person, i.e. the cases most at risk of being wrong)
"""
from __future__ import annotations

import importlib.util
import os
import statistics
import sys
import types
from collections import Counter
from pathlib import Path

import cv2

BTP = Path(__file__).resolve().parents[1]
SENIOR = Path(
    os.environ.get("UPSTREAM_BACKEND", BTP.parent / "upstream-vision" / "Backend")
)
VIDEO = Path(os.environ.get("EVAL_VIDEO", SENIOR / "app" / "uploads" / "test.mp4"))
WEIGHTS = Path(os.environ.get("EVAL_WEIGHTS", SENIOR / "yolo_models" / "yolo11s.pt"))

sys.path.insert(0, str(BTP))
from app.vision.vision_adapter import (  # noqa: E402
    MIN_CONTAINMENT,
    associate_ppe_frame,
    decode_detections,
)


def load_upstream(module: str, directory: Path, pkg: str):
    if pkg not in sys.modules:
        p = types.ModuleType(pkg)
        p.__path__ = [str(directory)]
        sys.modules[pkg] = p
    spec = importlib.util.spec_from_file_location(f"{pkg}.{module}", directory / f"{module}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{pkg}.{module}"] = mod
    spec.loader.exec_module(mod)
    return mod


yd = load_upstream("yolo_detector", SENIOR / "app" / "models", "sv")

STEP = 5     # every 5th frame -> 435 samples over 87 s, ~5 Hz
print("=" * 76)
print("EVALUATION ON REAL FOOTAGE")
print("=" * 76)
print(f"video   : {VIDEO.name}")
print(f"weights : {WEIGHTS.name}  (his better model)")

det = yd.YOLODetector(str(WEIGHTS))
names = dict(det.model.names)

cap = cv2.VideoCapture(str(VIDEO))
total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
print(f"sampling every {STEP}th frame of {total}\n")

persons_per_frame: list[int] = []
ppe_per_frame: list[int] = []
track_first: dict[int, int] = {}
track_last: dict[int, int] = {}
track_hits: Counter = Counter()

person_frames = 0          # total (person, frame) observations
decided = Counter()        # item -> observations with a True/False decision
contested = Counter()      # item -> PPE boxes claimable by >1 person
ppe_boxes_total = Counter()
ppe_assigned = Counter()
missing_calls = Counter()  # item -> observations reported MISSING

sampled = 0
for f in range(0, total, STEP):
    cap.set(cv2.CAP_PROP_POS_FRAMES, f)
    ok, frame = cap.read()
    if not ok:
        continue
    sampled += 1
    resized = cv2.resize(frame, (640, 480))
    raw = det.detect(resized.copy())

    decoded = decode_detections(raw, names)
    persons = [d for d in decoded if d.kind == "PERSON"]
    ppes = [d for d in decoded if d.kind == "PPE"]

    persons_per_frame.append(len(persons))
    ppe_per_frame.append(len(ppes))

    for p in persons:
        if p.track_id is not None:
            track_first.setdefault(p.track_id, f)
            track_last[p.track_id] = f
            track_hits[p.track_id] += 1

    for b in ppes:
        ppe_boxes_total[b.ppe_item] += 1

    if not persons:
        continue

    results = associate_ppe_frame(persons, ppes)
    person_frames += len(persons)
    for r in results:
        d = r.model_dump()
        for item in ("helmet", "vest", "mask"):
            if d[item] is not None:
                decided[item] += 1
                if d[item] is False:
                    missing_calls[item] += 1

    # contention: how many PPE boxes pass the containment gate for >1 person
    from app.vision.vision_adapter import containment
    for b in ppes:
        claimers = sum(
            1 for p in persons if containment(b.bbox, p.bbox) >= MIN_CONTAINMENT
        )
        if claimers > 1:
            contested[b.ppe_item] += 1
        if claimers >= 1:
            ppe_assigned[b.ppe_item] += 1

cap.release()

# ------------------------------------------------------------------ report
print("-" * 76)
print("1. DETECTION YIELD")
print("-" * 76)
nz = [n for n in persons_per_frame if n]
print(f"frames sampled            : {sampled}")
print(f"frames containing a person: {len(nz)}  ({100*len(nz)/sampled:.1f}%)")
print(f"persons per frame         : mean {statistics.mean(persons_per_frame):.2f}, "
      f"max {max(persons_per_frame)}")
if nz:
    print(f"  ...when present         : mean {statistics.mean(nz):.2f}")
print(f"PPE boxes per frame       : mean {statistics.mean(ppe_per_frame):.2f}, "
      f"max {max(ppe_per_frame)}")

print()
print("-" * 76)
print("2. TRACK FRAGMENTATION  (how well track_id works as worker identity)")
print("-" * 76)
lives = {t: (track_last[t] - track_first[t]) / 25.0 for t in track_first}   # 25 fps
print(f"distinct track_ids issued : {len(track_first)}")
if lives:
    vals = sorted(lives.values())
    print(f"track lifetime (s)        : median {statistics.median(vals):.1f}, "
          f"max {max(vals):.1f}, min {min(vals):.1f}")
    short = sum(1 for v in vals if v < 2.0)
    print(f"tracks shorter than 2 s   : {short} / {len(vals)}  "
          f"({100*short/len(vals):.0f}%)")
    print(f"observations per track    : median {statistics.median(sorted(track_hits.values()))}")
    print("\n  longest-lived tracks:")
    for t, v in sorted(lives.items(), key=lambda kv: -kv[1])[:8]:
        print(f"    #{t:<5} {v:>6.1f}s   {track_hits[t]:>3} observations")

print()
print("-" * 76)
print("3. PPE ASSOCIATION")
print("-" * 76)
print(f"person-frame observations : {person_frames}")
print(f"\n{'item':<9}{'decided':>9}{'coverage':>10}{'called MISSING':>16}")
print("-" * 46)
for item in ("helmet", "vest", "mask"):
    cov = 100 * decided[item] / person_frames if person_frames else 0
    print(f"{item:<9}{decided[item]:>9}{cov:>9.1f}%{missing_calls[item]:>16}")

print(f"\n{'item':<9}{'boxes':>8}{'attached':>10}{'contested':>11}  (contested = claimable by >1 person)")
print("-" * 60)
for item in ("helmet", "vest", "mask"):
    tot = ppe_boxes_total[item]
    con = 100 * contested[item] / tot if tot else 0
    print(f"{item:<9}{tot:>8}{ppe_assigned[item]:>10}{contested[item]:>7} ({con:>4.1f}%)")

total_boxes = sum(ppe_boxes_total.values())
total_contested = sum(contested.values())
print(f"\noverall contention rate   : {total_contested}/{total_boxes} = "
      f"{100*total_contested/max(total_boxes,1):.1f}% of PPE boxes")
print("  -> these are the observations where exclusive assignment matters and")
print("     where a wrong attribution is most likely.")
