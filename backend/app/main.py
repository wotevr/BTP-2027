"""
BTP-2027 - Spatial Safety Layer.

Boundary reminder:
    Construction_Safety_BTP  ->  WHO is this worker, what PPE are they wearing
    BTP-2027 (this app)      ->  WHERE are they standing, and is that safe

This process never runs YOLO and never opens a camera. It consumes normalised
detections over HTTP and turns them into positions, geofence events and alerts.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import (
    routes_alerts,
    routes_analytics,
    routes_calibration,
    routes_ingestion,
    routes_workers,
    routes_zones,
)
from app.core.config import settings
from app.core.websocket_manager import manager
from app.database.database import init_db
from app.services.alert_service import alert_service
from app.services.demo_simulator import demo_simulator
from app.services.fusion_engine import fusion_engine
from app.services.homography import calibration
from app.services.persistence import db_writer, prune_old_telemetry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("btp2027")

HEARTBEAT_SECONDS = 5.0


async def _housekeeping() -> None:
    """
    Periodic tick, independent of the detection feed.

    Without this, an alert would stay ACTIVE forever if the camera feed simply
    stopped - the auto-resolve only runs when a frame arrives.
    """
    while True:
        try:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            alert_service.tick()
            for resolved in alert_service.drain_resolved():
                from app.services.persistence import ResolveRecord

                db_writer.submit(ResolveRecord(resolved.event_id, resolved.resolved_at))
            if manager.client_count:
                await manager.broadcast(fusion_engine.site_state())
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001
            log.exception("Housekeeping tick failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting %s v%s", settings.APP_NAME, settings.VERSION)

    init_db()
    log.info("Database ready: %s", settings.sqlalchemy_url)

    try:
        pruned = prune_old_telemetry()
        if pruned:
            log.info("Pruned %d old telemetry row(s)", pruned)
    except Exception:  # noqa: BLE001 - housekeeping must never block startup
        log.exception("Telemetry pruning failed")

    calibration.load(settings.homography_path)
    if calibration.available:
        err = calibration.transform.reprojection_error()
        log.info(
            "Spatial calibration loaded (mean reprojection error %.2f px)",
            err if err is not None else float("nan"),
        )
    else:
        log.warning(calibration.error)

    skipped = fusion_engine.reload_zones()
    if skipped:
        log.warning("Zones skipped because of invalid geometry: %s", skipped)

    await db_writer.start()
    housekeeping = asyncio.create_task(_housekeeping(), name="housekeeping")

    if settings.DEMO_MODE:
        log.info("DEMO_MODE is on - starting the simulator")
        await demo_simulator.start()

    try:
        yield
    finally:
        log.info("Shutting down")
        housekeeping.cancel()
        await demo_simulator.stop()
        await manager.close_all()
        await db_writer.stop()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.VERSION,
    description=(
        "Spatial worker tracking and geofencing for construction sites. "
        "Consumes normalised detections from the upstream YOLO + BoT-SORT "
        "pipeline and answers where each worker is and whether that is safe."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in (
    routes_ingestion,
    routes_workers,
    routes_alerts,
    routes_zones,
    routes_analytics,
    routes_calibration,
):
    app.include_router(module.router, prefix=settings.API_PREFIX, tags=["API"])


@app.get("/", tags=["Health"])
async def root() -> dict:
    return {
        "name": settings.APP_NAME,
        "version": settings.VERSION,
        "docs": "/docs",
        "websocket": "/ws/live",
        "demo_mode": demo_simulator.running,
        "calibrated": calibration.available,
    }


@app.get("/health", tags=["Health"])
async def health() -> dict:
    return {"status": "ok", "frames_processed": fusion_engine.frames_processed}


@app.websocket("/ws/live")
async def websocket_live(websocket: WebSocket) -> None:
    """
    Live site state.

    The client gets a full snapshot on connect, then a fresh snapshot whenever
    the site changes. Update rate follows the upstream pipeline - if that runs
    at 8 FPS, this pushes about 8 messages a second, capped by
    BROADCAST_MAX_FPS. Nothing is fabricated to hit a nicer-looking number.

    The client may send:
        {"type": "ping"}   -> {"type": "pong"}
        {"type": "state"}  -> a fresh snapshot on demand
    """
    await manager.connect(websocket)
    try:
        await manager.send_personal(websocket, fusion_engine.site_state())
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await manager.send_personal(
                    websocket, {"type": "error", "message": "payload is not valid JSON"}
                )
                continue
            if not isinstance(message, dict):
                await manager.send_personal(
                    websocket, {"type": "error", "message": "payload must be an object"}
                )
                continue

            kind = message.get("type")
            if kind == "ping":
                await manager.send_personal(websocket, {"type": "pong"})
            elif kind == "state":
                await manager.send_personal(websocket, fusion_engine.site_state())
            else:
                await manager.send_personal(
                    websocket, {"type": "error", "message": f"unknown message type: {kind!r}"}
                )
    except WebSocketDisconnect:
        await manager.disconnect(websocket)
    except Exception:  # noqa: BLE001 - one bad client must not affect the rest
        log.exception("WebSocket client failed")
        await manager.disconnect(websocket)


@app.exception_handler(ValueError)
async def value_error_handler(_request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})
