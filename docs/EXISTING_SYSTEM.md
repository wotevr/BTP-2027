# The Existing System

### A technical review of `Construction_Safety_BTP`, and the limitation that motivates this project

> Material for the *Existing Work* / *Literature and Prior Work* chapter.
> Every number here was measured directly by running the system, not quoted
> from its documentation. The measurement script is
> [`backend/scripts/evaluate_on_footage.py`](../backend/scripts/evaluate_on_footage.py)
> and the results reproduce.

---

## 1. Scope of the review

The prior work under review is
[`darkm4tter07/Construction_Safety_BTP`](https://github.com/darkm4tter07/Construction_Safety_BTP),
a real-time construction-site safety monitor. It was developed independently
and is treated here as an **upstream system**: this project consumes its output
and does not modify it.

A verbatim copy is vendored at [`upstream-vision/`](../upstream-vision/) so the
version reviewed here is pinned and inspectable. Commit reviewed: `4c3f06e`
(2026-08-18).

---

## 2. System architecture

Four components, three of them processes:

| Component | Stack | Port | Role |
|---|---|---:|---|
| AI backend | FastAPI + YOLO11 + MediaPipe | 8000 | detection, tracking, pose, ergonomics |
| Frontend | React 19 + Vite + Tailwind | 5173 | operator dashboard |
| Auth server | Node + Express + Prisma | 3000 | Google OAuth, JWT issuance |
| Database + storage | Supabase (Postgres + Storage) | — | users, assessments, fitness, profile images |

Both back-end services share one Postgres database: the Python side owns the
schema through Alembic, and Prisma on the Node side is introspected from it.

The AI backend is the only component that needs a GPU.

### Deployment

The frontend is deployed to Vercel and the auth server to Render. Notably, the
frontend's service constants point the AI backend at the operator's own
machine:

```js
// Frontend/src/Constant.js
export const AUTH_URL = 'https://btp-auth-server-8dnd.onrender.com';
export const AI_URL   = 'http://localhost:8000';
```

So the deployment model is a **hosted UI driving a local inference process** —
sensible, since the GPU is on site and video never leaves the premises.

---

## 3. The vision pipeline

### 3.1 Model

Two sets of trained weights are committed under `Backend/yolo_models/`. Despite
being named after the stock Ultralytics releases, **neither is a COCO model** —
both are custom-trained on construction PPE. Read directly from the pickled
class tables:

| Index | `yolo11n.pt` (10 classes) | `yolo11s.pt` (11 classes) |
|---:|---|---|
| 0 | Hardhat | Hardhat |
| 1 | Mask | Mask |
| 2 | NO-Hardhat | NO-Hardhat |
| 3 | NO-Mask | NO-Mask |
| 4 | NO-Safety Vest | NO-Safety Vest |
| 5 | **Person** | **Person** |
| 6 | Safety Cone | Safety Cone |
| 7 | Safety Vest | Safety Vest |
| 8 | machinery | machinery |
| 9 | vehicle | *utility pole* |
| 10 | — | vehicle |

The class list is a variant of the widely used Roboflow *Construction Site
Safety* dataset. Two properties matter downstream:

* The model emits **explicit negative classes** (`NO-Hardhat`, `NO-Mask`,
  `NO-Safety Vest`). Absence of PPE is therefore a *detection*, not an
  inference from missing evidence — a meaningful design choice that this
  project preserves.
* The two weight files **disagree from index 9 upward**. Any consumer that maps
  by integer index is silently wrong on one of them.

### 3.2 Detection and tracking

```python
# Backend/app/models/yolo_detector.py
results = self.model.track(
    frame, device=self.device, persist=True,
    tracker="botsort.yaml", verbose=False, conf=0.1,
)
```

* **Tracker:** BoT-SORT, through Ultralytics.
* `persist=True` carries tracker state between calls, so `track_id` is intended
  to be stable for the lifetime of a stream.
* `conf=0.1` is a deliberately permissive threshold, favouring recall over
  precision — appropriate for safety, where a missed worker is worse than a
  spurious box.

Each detection is returned as:

```python
{"bbox": [x1, y1, x2, y2], "conf": 0.94, "class_id": 5, "track_id": 17}
```

### 3.3 Frame handling

```python
# Backend/app/models/safety_monitor.py
frame_resized = cv2.resize(frame, (640, 480))
detections = self.yolo.detect(frame_resized.copy())
```

Every frame is resized to **640 × 480 before inference**, so all coordinates
live in that space regardless of camera resolution. Any downstream spatial
calibration must be performed in the same space — a detail that is easy to miss
and fatal to get wrong.

Throughput is paced at roughly **10 FPS** (`time.sleep(0.1)` in
`cctv_service.py`).

### 3.4 Beyond detection

The system is broader than PPE:

* **Pose estimation** — MediaPipe landmarks per frame.
* **Ergonomic scoring** — RULA and REBA computed from those landmarks
  (`ergonomic_analyzer.py`), giving a posture-risk score per worker.
* **Worker fitness** — Google Fit integration (steps, heart rate, calories),
  which is why the OAuth flow requests `fitness.*` scopes.
* **Cognitive assessment** — periodic fitness-for-work testing with expiry.
* **Weather** — OpenWeatherMap, with heat-stress alerting.

The operator dashboard presents these as parallel panels: Object Detection, PPE
Compliance, Pose Detection, Ergonomics, Worker Fitness, Weather, and Safety
Alerts, with CCTV input configurable as RTSP/IP camera, video file, or HLS.

---

## 4. Measured performance

Measured on the system's own bundled footage
(`Backend/app/uploads/test.mp4`): 586 × 480, 87 s, 25 fps, genuine site CCTV
dated 29-01-2021. Inference with `yolo11s.pt`, sampling every 5th frame
(435 samples).

### 4.1 Detection yield

| Metric | Value |
|---|---:|
| Frames containing at least one person | 60.7% |
| Persons per frame, overall mean | 1.00 |
| Persons per frame, when present | 1.65 |
| Maximum persons in a frame | 4 |
| PPE boxes per frame, mean | 1.45 |

### 4.2 Detections by class

Over a 145-frame sweep:

| Class | Count | Mean conf. | Max conf. |
|---|---:|---:|---:|
| Person | 136 | 0.667 | 0.925 |
| machinery | 122 | 0.332 | 0.855 |
| Safety Vest | 89 | 0.730 | 0.880 |
| vehicle | 33 | 0.392 | 0.698 |
| Hardhat | 32 | 0.557 | 0.785 |
| NO-Safety Vest | 9 | 0.487 | 0.757 |
| Safety Cone | 8 | 0.181 | 0.378 |
| NO-Mask | 6 | 0.424 | 0.679 |
| NO-Hardhat | 2 | 0.337 | 0.428 |

Person and Safety Vest detection is strong. The negative classes fire rarely,
which is expected on footage where workers are largely compliant, but it means
violation events are sparse and hard to evaluate on this clip.

### 4.3 Model selection

The configured default is the smaller model:

```python
# Backend/app/core/config.py
YOLO_MODEL_PATH: str = "yolo_models/yolo11n.pt"
```

Measured against the larger weights on identical frames:

| | `yolo11n.pt` | `yolo11s.pt` |
|---|---:|---:|
| Person detections | 108 | **136** |
| Frames with a person | 68 / 145 | **86 / 145** |
| PPE detections | negligible | 136 |

**`yolo11s.pt` is materially better and should be the default.** On the smaller
model, PPE is barely detected at all — which would make PPE compliance
reporting effectively non-functional.

---

## 5. Structural findings

Five observations that bear on any system built on top of this one.

### 5.1 PPE is never associated with a person

This is the most consequential finding. The model detects PPE as **independent
bounding boxes**. Nothing in the pipeline links a `Safety Vest` box to a
`Person` box. The dashboard aggregates them globally:

```js
// Frontend/src/utils/detectionUtils.js
hardhatCount: detections.filter(d => d.class_id === CLASS_IDS.HARDHAT).length,
```

So the system can report *"3 people, 2 hardhats"* but cannot report *"worker
#17 is not wearing a hardhat."* Per-worker PPE state does not exist upstream.

### 5.2 Worker identity is assigned manually

`worker_tracking_service.py` maps a `track_id` to a worker profile only when an
administrator clicks a bounding box and selects a person from a dropdown. There
is no automatic re-identification. A track is considered lost after 60
consecutive missing frames, at which point the mapping is discarded.

### 5.3 Person class is hard-coded by index

```python
person_detections = [d for d in detections if d.get("class_id") == 5]
```

Correct for both current weight files, but silently wrong for any retrained
model with a different class order.

### 5.4 Import-time side effects

`Backend/app/models/__init__.py` instantiates the full `SafetyMonitor` — loading
the YOLO model and MediaPipe — as a side effect of importing the package. Any
consumer wanting only the detector inherits the entire dependency tree.

### 5.5 Environment portability

`requirements.txt` pins `ultralytics==8.3.0`, which resolves numpy to 1.26.4.
That release has no Python 3.13 wheel, so installation on 3.13 attempts a
source build and fails. **The stack is Python 3.12-only as pinned.** The
database models additionally use PostgreSQL-specific column types
(`postgresql.UUID`, `JSONB`), so SQLite cannot be substituted for local
development.

---

## 6. The core limitation: track fragmentation

The single most important measured result.

| Metric | Value |
|---|---:|
| Distinct `track_id`s issued over 87 s | **19** |
| Visually distinct people in the footage | ~3–4 |
| Median track lifetime | **2.6 s** |
| Longest track | 21.2 s |
| Tracks shorter than 2 s | **47%** |
| Median observations per track | 6 |

BoT-SORT associates detections by appearance and motion. When a worker passes
behind a container, a fence panel, or a stack of materials — constant events on
a construction site — the track terminates. On re-emergence the worker is
assigned a **new** identity.

Three consequences:

1. **Attendance is not computable.** "How long was this worker on site" cannot
   be answered when one person is nineteen identities.
2. **Manual assignment does not survive.** An administrator who labels track
   #17 as a named worker loses that binding at the next occlusion, and must
   repeat it for #204, #331, and so on. This does not scale past a handful of
   workers.
3. **Time-based safety rules are undermined.** Any rule requiring a condition
   to persist — *"missing hardhat for more than 2 seconds"* — cannot fire for
   the 47% of tracks that do not survive that long. The debounce intended to
   suppress detector flicker silently suppresses genuine violations instead.

This is not a defect in the implementation. It is the inherent limit of
appearance-based tracking from a single fixed viewpoint, and no amount of
tuning removes it.

---

## 7. What this project adds, and why

The review above isolates two gaps that are not addressable within the existing
architecture:

**Gap 1 — no spatial grounding.** The system knows a worker occupies pixels
`[412, 183, 498, 421]`. It does not know where that is on the site. Safety
rules — *stay 5 m from the excavator*, *do not enter the excavation* — are
expressed in metres, and the pipeline has no metre.

> **Addressed by:** homography-based ground-plane localisation. The
> bottom-centre of each bounding box is the worker's ground-contact point;
> a projective transform calibrated from four measured correspondences maps it
> to site coordinates in metres, and zone membership becomes a point-in-polygon
> test.

**Gap 2 — identity does not persist.** Measured at 19 identities for ~4 people,
with 47% of tracks under 2 seconds.

> **Addressed by:** fusion with a second, independent position source. A GPS
> fix from a phone carried by the worker is coarse (3–10 m) but its identity is
> exact and uninterrupted. The camera supplies precision; GPS supplies
> continuity and identity. They fail under opposite conditions — the camera
> when occluded or out of frame, GPS when indoors or beside structures — which
> is what makes fusing them worthwhile rather than redundant.

The resulting division of responsibility:

| | Upstream system | This project |
|---|---|---|
| Question answered | *Who is this, and what PPE?* | *Where are they, and is it safe?* |
| Sensors | CCTV | CCTV + GPS |
| Identity | Manual, per track | Anchored, persistent |
| Position | Pixels | Metres |
| Safety rules | PPE presence | PPE + spatial geofencing |

---

## 8. Reproducing these measurements

```bash
cd backend
python scripts/evaluate_on_footage.py
```

Defaults to the vendored upstream copy and its bundled footage. Override with
`UPSTREAM_BACKEND`, `EVAL_VIDEO`, `EVAL_WEIGHTS`.

To view the upstream detector's own annotated output:

```bash
python scripts/yolo_bridge.py \
    --upstream ../upstream-vision/Backend \
    --weights ../upstream-vision/Backend/yolo_models/yolo11s.pt \
    --source ../upstream-vision/Backend/app/uploads/test.mp4 \
    --send-frames --loop
```

Boxes are drawn by the upstream `draw_detections`, so the rendering is the
detector's own.

---

## 9. Acknowledgement

The vision layer reviewed here — the trained model, detection, tracking, pose
estimation and ergonomic scoring — is the work of the author of
`Construction_Safety_BTP`. This project reuses it without modification and
claims no credit for it. See
[`upstream-vision/ATTRIBUTION.md`](../upstream-vision/ATTRIBUTION.md).
