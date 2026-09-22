# Attribution — vendored vision layer

Everything in this directory is a **verbatim copy** of

> https://github.com/darkm4tter07/Construction_Safety_BTP

It is **not my work.** It is included here only so that `wotevr/BTP-2027` runs
end to end from a single clone, and so the exact code the spatial safety layer
integrates against is pinned and inspectable.

Vendored on 2026-09-22.

## What it provides

| | |
|---|---|
| Model | YOLO11, custom-trained on construction PPE (`Backend/yolo_models/`) |
| Classes | Hardhat, Mask, NO-Hardhat, NO-Mask, NO-Safety Vest, **Person (5)**, Safety Cone, Safety Vest, machinery, vehicle (+ utility pole in the `s` weights) |
| Tracker | BoT-SORT via Ultralytics, `persist=True` → persistent `track_id` |
| Also | MediaPipe pose estimation and RULA/REBA ergonomic scoring |
| Output | `{"bbox": [x1,y1,x2,y2], "conf": …, "class_id": …, "track_id": …}` in a 640×480 pixel space |

## Changes made to this copy

Two, both purely mechanical:

1. `Backend/.gitattributes` removed. It declared `*.pt filter=lfs`, which would
   make the weights clone as broken LFS pointers for anyone without Git LFS
   configured. The weights here are committed as ordinary files.
2. `node_modules/` and `__pycache__/` excluded.

No source file was modified. The spatial safety layer in `backend/` integrates
with this code through an adapter and an HTTP boundary — see
[`../docs/INTEGRATION.md`](../docs/INTEGRATION.md).

## Running it

This layer has its own requirements (torch, ultralytics, mediapipe) and its own
`.env`. Follow `UPSTREAM_README.md`. It is **not** needed to run the BTP-2027
dashboard in demo mode — only to feed it real detections.
