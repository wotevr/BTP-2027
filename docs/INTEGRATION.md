# Integrating the existing vision pipeline

This document is the precise answer to "how do the two repositories connect".

| | |
|---|---|
| **Upstream (existing)** | [`darkm4tter07/Construction_Safety_BTP`](https://github.com/darkm4tter07/Construction_Safety_BTP) |
| **This repository** | `wotevr/BTP-2027` |

---

## 1. What the upstream repository actually produces

Read from the source, not assumed.

### Model

`Backend/app/core/config.py`

```python
YOLO_MODEL_PATH: str = "yolo_models/yolo11n.pt"
```

Two sets of weights are committed under `Backend/yolo_models/`. Despite the
filenames they are **not** the stock COCO YOLO11 models — they are custom-trained
construction-PPE models. Inspecting the pickled class table inside each file:

| index | `yolo11n.pt` (10 classes) | `yolo11s.pt` (11 classes) |
|---|---|---|
| 0 | Hardhat | Hardhat |
| 1 | Mask | Mask |
| 2 | NO-Hardhat | NO-Hardhat |
| 3 | NO-Mask | NO-Mask |
| 4 | NO-Safety Vest | NO-Safety Vest |
| 5 | **Person** | **Person** |
| 6 | Safety Cone | Safety Cone |
| 7 | Safety Vest | Safety Vest |
| 8 | machinery | machinery |
| 9 | vehicle | utility pole |
| 10 | — | vehicle |

The two disagree from index 9 upward. `Person` is index 5 in both, which is why
the upstream `worker_tracking_service` can hard-code `class_id == 5`. **This
repository maps by class *name* instead**, using `model.names` when available,
so either set of weights works and a future retrain does not silently break the
mapping.

### Detection and tracking

`Backend/app/models/yolo_detector.py`

```python
results = self.model.track(
    frame, device=self.device, persist=True,
    tracker="botsort.yaml", verbose=False, conf=0.1,
)
```

* Tracker: **BoT-SORT**, through Ultralytics.
* `persist=True` keeps track state across calls, so `track_id` is **stable for
  the lifetime of the stream**. This is exactly what the spatial layer needs and
  it is reused unchanged — BTP-2027 contains no tracker of its own.

Each detection comes back as:

```python
{"bbox": [x1, y1, x2, y2], "conf": 0.94, "class_id": 5, "track_id": 17}
```

### Pixel space — the detail that matters most

`Backend/app/models/safety_monitor.py`

```python
frame_resized = cv2.resize(frame, (640, 480))
detections = self.yolo.detect(frame_resized.copy())
```

Every frame is resized to **640×480 before inference**, so all boxes are in that
pixel space regardless of the camera resolution. The homography is therefore
calibrated in 640×480 as well. If you change the upstream resize, either
recalibrate or send the real `frame_width`/`frame_height` and the backend will
rescale for you (`HomographyTransform.scale_pixel`).

### Throughput

`Backend/app/services/cctv_service.py` sleeps `0.1 s` per frame, i.e. roughly
**10 FPS**. BTP-2027 broadcasts at whatever rate detections actually arrive; it
never fabricates a higher number.

---

## 2. The one gap, stated honestly

**The upstream pipeline does not produce per-worker PPE.**

It emits PPE as *independent boxes*. Nothing links a `Hardhat` box to a `Person`
box. The existing dashboard only counts them globally:

```js
// Frontend/src/utils/detectionUtils.js
hardhatCount: detections.filter(d => d.class_id === CLASS_IDS.HARDHAT).length,
```

So a record like `{"track_id": 17, "ppe": {"helmet": true}}` **does not exist
upstream and cannot simply be copied**.

BTP-2027 derives it in `backend/app/vision/vision_adapter.py` by **spatial
association of the boxes upstream already produced**. No second model, no
retraining, no extra inference — pure geometry:

```
score(person, ppe) = containment(ppe, person)     # fraction of the PPE box inside the person box
                   × band_score(item, ppe, person) # is a hardhat near the head?
                   × detector_confidence
```

Pairs are scored across the whole frame and assigned greedily, **exclusively**:
one PPE box can belong to only one person. Without that, two workers standing
shoulder to shoulder would both be credited with the same helmet.

Known limits — say these out loud in the viva rather than hiding them:

* Heavy occlusion or crowding can still mis-attribute a box. `MIN_CONTAINMENT`
  and the vertical band check reduce this, they do not eliminate it.
* "Missing" comes from the model's own `NO-*` classes. It is the detector's
  judgement, not an inference drawn from absence.
* An item nothing was observed for stays `null` (**unknown**), never `false`.
  Absence of evidence is never treated as a violation.

---

## 3. The normalised contract

Everything downstream of the adapter speaks only this:

```json
{
  "timestamp": "2026-09-22T10:30:15.200Z",
  "camera_id": "camera_01",
  "frame_id": 12482,
  "frame_width": 640,
  "frame_height": 480,
  "workers": [
    {
      "track_id": 17,
      "bbox": [412, 183, 498, 421],
      "confidence": 0.94,
      "ppe": { "helmet": true, "vest": false, "mask": null }
    }
  ]
}
```

`ppe` values are tri-state: `true` worn, `false` explicitly detected missing,
`null` not observed.

Nothing below the adapter imports `ultralytics`, `torch`, or any tracker
internals. That is the architectural boundary.

---

## 4. Three ways to connect them

### Option A — the bridge script (recommended, zero changes upstream)

`backend/scripts/yolo_bridge.py` runs **in the upstream environment**, imports
the existing `YOLODetector` untouched, and posts what it returns.

```bash
# in the Construction_Safety_BTP environment (the one with torch + ultralytics)
python yolo_bridge.py \
    --upstream /path/to/Construction_Safety_BTP/Backend \
    --source 0 \
    --api http://localhost:8000
```

It is standalone on purpose: both repositories have a top-level package called
`app`, so importing them into one interpreter would collide. Keeping them in
separate processes also means the BTP-2027 API needs no GPU dependencies.

Check the wiring with no camera and no model at all:

```bash
python yolo_bridge.py --self-test
```

### Option B — push from upstream (about 15 lines)

If you would rather the existing backend push directly, add this to
`Backend/app/models/safety_monitor.py`:

```python
import requests, threading
from datetime import datetime, timezone

BTP2027_URL = "http://localhost:8000/api/v1/detections/raw"
_session = requests.Session()

def push_to_spatial_layer(detections, class_names, frame_id):
    """Fire-and-forget: the spatial layer must never slow down inference."""
    payload = {
        "camera_id": "camera_01",
        "frame_id": frame_id,
        "frame_width": 640,          # the size safety_monitor resizes to
        "frame_height": 480,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "class_names": {str(k): v for k, v in class_names.items()},
        "detections": detections,
    }
    try:
        _session.post(BTP2027_URL, json=payload, timeout=1.0)
    except requests.RequestException:
        pass  # the safety layer being down must not break the camera feed
```

then, at the end of `process_frame`, after `detections` exist:

```python
threading.Thread(
    target=push_to_spatial_layer,
    args=(detections, self.yolo.model.names, self._frame_id),
    daemon=True,
).start()
```

Use a thread (or a queue) so a slow HTTP call never blocks the detector.

### Option C — already-normalised payloads

If you have built the per-worker PPE yourself, POST to `/api/v1/detections`
with the contract in section 3 and the adapter is skipped entirely.

---

## 5. What happens next, inside BTP-2027

```
POST /api/v1/detections/raw
  -> vision_adapter.normalize()       class mapping + exclusive PPE association
  -> DetectionFrame                   the normalised contract
  -> fusion_engine.process_frame()
       foot point        (x1+x2)/2, y2        bottom-centre, not the box centre
       homography        pixel -> local site metres
       geofence          Shapely covers() against active zone polygons
       safety events     debounced zone breaches + PPE violations
       telemetry         sampled, queued, written off the hot path
       broadcast         WS /ws/live
```

The response tells you what happened:

```json
{
  "success": true,
  "processed_workers": 1,
  "alerts_generated": 1,
  "spatial_calibration": true,
  "detail": null
}
```

If `spatial_calibration` is `false`, `detail` explains why and workers are still
tracked in pixel space — the system degrades, it does not fail.

---

## 6. What was deliberately NOT rebuilt

| Component | Where it lives | Why |
|---|---|---|
| YOLO11 PPE model | upstream | Already trained on the right classes |
| Person detection | upstream | Class 5 of the same model |
| PPE detection | upstream | Classes 0–4, 7 of the same model |
| BoT-SORT tracking | upstream | Already gives persistent `track_id` |
| Persistent worker ids | upstream | Consumed as-is, never regenerated |
| MediaPipe pose / RULA / REBA | upstream | Ergonomics, a separate concern from location |
| Worker identity and auth | upstream | BTP-2027 keys on `track_id` only |

BTP-2027 adds only what did not exist: foot-point extraction, homography,
world coordinates, geofencing, the safety event engine, telemetry, attendance,
and the spatial dashboard.

---

## 7. A known limitation worth owning

Identity here **is** the tracker's `track_id`. If BoT-SORT loses a worker behind
a column and re-acquires them with a new id, the system sees a new worker:
attendance restarts and zone memory resets.

Cross-break re-identification (appearance embeddings, or linking a track id to a
real worker profile — which the upstream `worker_tracking_service` already has a
UI for) is the obvious next extension, and is out of scope here.
