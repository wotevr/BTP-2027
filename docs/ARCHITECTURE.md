# Architecture

**Integrated Site Monitor — fusing spatial and visual data for worker safety and productivity.**

This document is the plan of record. It marks every component as
**`[EXISTING]`** (the upstream vision system, reused unchanged),
**`[NEW]`** (this project), or **`[INTEGRATION]`** (the adapter between them),
so that the boundary between prior work and this project is never ambiguous.

---

## 1. The problem in one line

A CCTV camera sees a worker in **pixels**. Safety rules are written in
**metres**. Nothing in the existing system bridges that gap.

```
            pixels                                metres
    [412, 183, 498, 421]   ─── ??? ───►   "5 m from the excavator"
```

Everything below exists to build, and then exploit, that bridge.

---

## 2. System overview

```
┌─ EXISTING ─────────────────┐   ┌─ NEW ────────────────────────────────────┐
│                            │   │                                          │
│  video ─► YOLO11 detect    │   │   feet point ─► per-camera homography ─┐  │
│        ─► BoT-SORT track   ├──►│                                        │  │
│        ─► PPE classes      │   │   GPS lat/lon ─► site geo-reference ───┤  │
│        ─► MediaPipe pose   │   │                                        │  │
│        ─► RULA/REBA        │   │              COMMON SITE FRAME (m) ◄───┘  │
└────────────────────────────┘   │                       │                   │
                                 │          ┌────────────┴────────────┐      │
                                 │      geofencing              productivity │
                                 │          │                          │     │
                                 │      event engine            dwell/paths  │
                                 │          └────────────┬────────────┘      │
                                 │                       ▼                   │
                                 │           database ─► WebSocket ─► map    │
                                 └──────────────────────────────────────────┘
```

The single invariant of this architecture:

> **Every spatial quantity — worker positions, zone polygons, camera coverage —
> is ultimately expressed as `(site_x, site_y)` in metres, relative to one
> origin per site.**

Pixels exist only inside a camera's own adapter. Latitude/longitude exists only
inside the GPS adapter. Neither leaks into the rest of the system.

---

## 3. What already exists

### 3.1 `[EXISTING]` Upstream vision — `upstream-vision/`

Vendored verbatim from
[`darkm4tter07/Construction_Safety_BTP`](https://github.com/darkm4tter07/Construction_Safety_BTP).
**Not rewritten.** See [`EXISTING_SYSTEM.md`](EXISTING_SYSTEM.md) for the
measured review.

| Component | Detail |
|---|---|
| Detector | YOLO11, custom-trained. Classes 0–9: Hardhat, Mask, NO-Hardhat, NO-Mask, NO-Safety Vest, **Person (5)**, Safety Cone, Safety Vest, machinery, vehicle |
| Tracker | BoT-SORT via Ultralytics, `persist=True` → `track_id` |
| Inference space | Frames resized to **640×480** before detection |
| Output | `{"bbox": [x1,y1,x2,y2], "conf": …, "class_id": …, "track_id": …}` |
| Also | MediaPipe pose, RULA/REBA ergonomics, weather, fitness |

### 3.2 `[EXISTING]` Upstream auth — `BTP-auth-server`

Node + Express + Prisma on Postgres. Google OAuth, JWT issuance, Google Fit
sync. Its data model owns **worker identity**:

`users` (id, email, full_name, role ADMIN/WORKER, employee_id, google_id) ·
`worker_profiles` (HR/medical/certification data) · `cognitive_assessments` ·
`fitness_connections` · `fitness_data`

**Decision: this project does not duplicate or depend on it.** BTP-2027 keeps
its own lightweight `Worker` record carrying an optional `external_user_id`, so
the two can be joined later without coupling the spatial pipeline to an
external Postgres instance and an OAuth flow.

### 3.3 `[NEW, already built]` Spatial core

Built in earlier phases of this project and carried forward:

| Module | Responsibility |
|---|---|
| `services/homography.py` | pixel ↔ metres, calibration validation, reprojection error |
| `services/geofence_engine.py` | Shapely polygons, `covers()` semantics, enter/exit hysteresis |
| `services/alert_service.py` | event lifecycle, dwell thresholds, cooldowns |
| `services/fusion_engine.py` | per-frame orchestration |
| `services/persistence.py` | async batched writes, never blocks the hot path |
| `vision/vision_adapter.py` | upstream output → normalised schema, exclusive PPE association |
| `services/frame_buffer.py` | latest annotated frame per camera, MJPEG |

131 tests pass against these.

---

## 4. What the audit found wrong

The existing implementation is **implicitly single-site and single-camera**.
The spec requires multi-site, multi-camera. Concretely:

| Problem | Consequence | Fix |
|---|---|---|
| No `Site` entity | Zones, cameras and workers float free | **Phase 1** |
| No `Camera` entity — `camera_id` is a bare string | Cannot register, calibrate or status-check a camera | **Phase 3** |
| **One global homography** loaded from a single JSON file | Every camera would share one calibration — architecturally wrong | **Phase 4** |
| `worker_id` **is** the BoT-SORT `track_id` | Violates §18. One person becomes many workers; attendance is meaningless | **Phase 9** |
| Zone polygons typed as JSON | Manager cannot draw on a plan | **Phase 5** |
| No site plan | Map background is a static file | **Phase 2** |
| No position source abstraction | GPS/UWB cannot be added | **Phase 12** |

The last one in the middle is the most important: **`track_id ≠ worker_id`.**
The measured evidence is in `EXISTING_SYSTEM.md` — 19 track ids issued for
~4 people over 87 s, 47 % of tracks shorter than 2 s. A data model that equates
them cannot represent reality.

---

## 5. Target data model `[NEW]`

```
Site ──┬── SitePlan            (uploaded drawing + georeference)
       ├── Camera ──── CameraCalibration   (homography, per camera)
       ├── Zone                (polygon in SITE metres)
       ├── Worker ──┬── Shift
       │            └── WorkerDevice
       └── SafetyEvent

PositionObservation  ← the unified spatial record
TrackAssociation     ← links a camera track to a worker identity
```

### The two keystone tables

**`PositionObservation`** — every position, from every source, in one shape:

| field | note |
|---|---|
| `site_id`, `x`, `y` | **always site metres** |
| `source` | `camera` · `gps` · `uwb` · `ble` · `dev` |
| `accuracy_m` | preserved, never discarded |
| `camera_id`, `track_id` | when source is a camera |
| `worker_id` | when identity is known — **nullable** |
| `observed_at` | source timestamp, not arrival time |

**`TrackAssociation`** — the deliberate seam between tracking and identity:

| field | note |
|---|---|
| `camera_id`, `track_id` | what the tracker saw |
| `worker_id` | who we believe it is |
| `confidence`, `method` | `manual` · `gps_proximity` · `reid` |
| `valid_from`, `valid_to` | associations expire; tracks die |

A camera track with no association is still tracked and still geofenced — it is
simply an **anonymous** worker. That is the honest representation.

---

## 6. The two coordinate bridges

Both end in the same frame. They are **different transforms** and must not be
confused.

```
CAMERA                                    GPS
  bbox [x1,y1,x2,y2]                        latitude, longitude
        │                                          │
  feet = ((x1+x2)/2, y2)                    site geo-reference
        │  ground-contact point                    │  (origin lat/lon + rotation)
        │                                          │
  per-camera homography H                   local ENU projection
        │  3×3, calibrated from 4 pts              │
        ▼                                          ▼
   (site_x, site_y)  ◄──── SAME FRAME ────►  (site_x, site_y)
   accuracy ~0.3–1 m                         accuracy 3–10 m
```

**Why the feet.** A homography is only valid for points *on the ground plane*.
The centre of a person's bounding box sits at roughly chest height and projects
to a point well behind where they stand.

**Why per-camera H.** Each camera sees the ground from a different position and
angle, so each needs its own transform. They all target the **same site origin**,
which is what makes one unified map possible.

---

## 7. Multi-camera → one map

```
CAM-01 ── H₁ ──┐
CAM-02 ── H₂ ──┤
CAM-03 ── H₃ ──┼──► (site_x, site_y) ──► ONE map the manager sees
CAM-04 ── H₄ ──┘
```

Different homographies, one coordinate system. Camera 3 does not need to know
camera 1 exists.

**Operational rule:** the site origin is chosen **once**, before any camera is
calibrated, and every camera's calibration points are measured against it.

**Explicitly not claimed:** spatial calibration does **not** synchronise cameras
in time. Every observation carries its own timestamp and cross-camera matching
is by space *and* time. Sub-frame temporal alignment is out of scope.

---

## 8. Position confidence, not position alone

The map must distinguish three states. Treating them identically would mislead
an operator into thinking an unmonitored area is a safe one.

| State | Rendering | Meaning |
|---|---|---|
| Camera-observed | solid marker | precise, ~sub-metre |
| GPS-observed | marker + uncertainty circle sized to `accuracy_m` | coarse |
| Not observed | greyed, "last seen HH:MM" | **unknown, not absent** |

Camera-blind regions are shaded on the map. **No detection is not evidence of
no worker.**

Fusion is **source selection with confidence**, not averaging. Positions from
sources with different error characteristics are never blindly averaged.

---

## 9. Two clients

| | Manager — web | Worker — mobile |
|---|---|---|
| Site setup, plan upload | ✅ | — |
| Camera registry + calibration | ✅ | — |
| Zone drawing | ✅ | — |
| Unified live map | ✅ | — |
| Events, history | ✅ | — |
| Login, start/end shift | — | ✅ |
| Location permission + GPS reporting | — | ✅ |

The worker client is deliberately minimal: identity, shift, permission,
tracking status. Nothing more.

**GPS is a real data path, not a database column:**

```
phone GPS ─► worker app ─► POST /api/v1/positions ─► geo-reference
          ─► PositionObservation ─► WebSocket ─► manager map
```

Tracking is bound to an **active shift**. Location is not collected outside one.

---

## 10. Event lifecycle

```
OUTSIDE ──enter──► CANDIDATE ──dwell exceeded──► ACTIVE ──exit──► RESOLVED
                       │                            │
                   (noise, discarded)        (evidence stops) ──► timeout
```

Detection noise must not become alert spam. Three mechanisms:
**hysteresis** (only enter/exit transitions), a **dwell threshold** before an
event becomes ACTIVE, and **one open event per condition** with an explicit
ACTIVE→RESOLVED lifecycle.

Because tracks are short-lived, the engine never assumes a track survives. An
event whose evidence stops arriving times out rather than hanging open forever.

---

## 11. Honest limitations

Stated here so they are never accidentally overclaimed.

1. **Calibration.** The homography pipeline is implemented and tested, but
   real-world metre accuracy requires calibration against an actual site. Until
   then coordinates are geometrically consistent, not physically meaningful.
   The UI shows calibration status per camera.
2. **GPS.** 3–10 m typical, worse near steel and structures. Sufficient for
   coarse location and identity continuity; **not** sufficient to resolve a 5 m
   zone. Precise positioning in camera-blind areas needs UWB (10–30 cm).
3. **Identity.** `track_id` is not a worker. Association is explicit,
   confidence-scored and expiring. Cross-camera re-identification is not
   claimed — construction workers in identical PPE are close to
   indistinguishable to an appearance model.
4. **Time.** Cameras are not time-synchronised by this system.
5. **DWG.** Native DWG parsing is not attempted. DXF, PDF and raster formats
   are supported; DWG requires external conversion.

---

## 12. Implementation phases

| Phase | Deliverable | State |
|---:|---|---|
| 0 | Audit + this document | ✅ |
| 1 | Site + SitePlan foundation | |
| 2 | Site map + coordinate system | |
| 3 | Camera registry | |
| 4 | Per-camera calibration + homography | |
| 5 | Zone editor on the plan | |
| 6 | Upstream AI → feet → site coordinates | |
| 7 | Unified multi-camera map | |
| 8 | Safety event engine | |
| 9 | Worker management + track/identity split | |
| 10 | Worker mobile client + GPS ingestion | |
| 11 | GPS → site coordinate conversion | |
| 12 | Confidence-aware position layer | |
| 13 | WebSocket live dashboard | |
| 14 | Testing | |
| 15 | Documentation and verification | |

Each phase is implemented, tested, run, verified and committed before the next
begins.

---

## 13. Development without hardware

Real CCTV, a real phone and a real site are not always available. Two clearly
separated development inputs exist, and both are labelled
**DEVELOPMENT SIMULATION** in the UI and in the API:

* **video file → upstream detector → ingestion** — exercises the real detection
  and spatial path with recorded footage
* **`/api/v1/dev/positions`** — submits position observations without a phone

Simulated data is never mixed into production state unlabelled, and production
views show honest empty states (`No cameras configured`, `No active workers`)
rather than placeholder numbers.
