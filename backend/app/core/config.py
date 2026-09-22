"""
Central configuration for the BTP-2027 spatial safety layer.

Everything is overridable from environment variables / a `.env` file so that
nothing about the deployment (camera source, database path, calibration file)
is hard-coded.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/  -> the directory that holds `app/`, `data/`, `scripts/`
BACKEND_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ------------------------------------------------------------------ app
    APP_NAME: str = "BTP-2027 Spatial Safety Monitor"
    VERSION: str = "1.0.0"
    API_PREFIX: str = "/api/v1"

    # ------------------------------------------------------------- database
    DATABASE_URL: str = "sqlite:///./data/btp.db"

    # ---------------------------------------------------------------- CORS
    # Comma-separated list, e.g. "http://localhost:5173,http://localhost:3000"
    ALLOWED_ORIGINS: str = "http://localhost:5173,http://localhost:3000,http://127.0.0.1:5173"

    # ---------------------------------------------------------- vision feed
    # Only used by the *optional* bridge process that drives the upstream
    # YOLO pipeline. The API itself never opens a camera.
    VIDEO_SOURCE: str = "0"
    CAMERA_ID: str = "camera_01"

    # The upstream pipeline resizes every frame to 640x480 before inference,
    # so detections arrive in that pixel space unless the payload says
    # otherwise. Calibration is stored against this same reference size.
    FRAME_WIDTH: int = 640
    FRAME_HEIGHT: int = 480

    # ----------------------------------------------------------- spatial
    HOMOGRAPHY_FILE: str = "data/calibration/homography.json"
    SITE_PLAN_FILE: str = "data/site_map/site_plan.json"

    # Site extent in metres, used by the dashboard to lay out the map.
    SITE_WIDTH_M: float = 30.0
    SITE_DEPTH_M: float = 20.0

    # --------------------------------------------------------- telemetry
    # Minimum seconds between two stored positions *for the same worker*.
    TELEMETRY_SAMPLE_INTERVAL: float = 0.5
    # Rows older than this are pruned on startup (0 disables pruning).
    TELEMETRY_RETENTION_HOURS: int = 72

    # ------------------------------------------------------------ alerts
    # A repeat of the same (worker, type, zone) is suppressed for this long.
    ALERT_COOLDOWN_SECONDS: float = 30.0
    # PPE must be missing for this long before an alert is raised. Protects
    # against single-frame detector flicker.
    PPE_VIOLATION_MIN_SECONDS: float = 2.0
    # An ACTIVE alert auto-resolves once its condition has been clear this long.
    ALERT_AUTO_RESOLVE_SECONDS: float = 10.0

    # PPE items that are mandatory on this site. Anything not listed is
    # tracked and displayed but never raises an alert.
    REQUIRED_PPE: str = "helmet,vest"

    # ---------------------------------------------------------- presence
    # Gap between two sightings longer than this is treated as the worker
    # having left, so the gap is NOT counted as time on site.
    PRESENCE_GAP_SECONDS: float = 15.0
    # No sighting for this long flips the worker to OFF_SITE.
    ABSENT_AFTER_SECONDS: float = 60.0

    # ------------------------------------------------------------- demo
    DEMO_MODE: bool = False
    DEMO_FPS: float = 8.0

    # ------------------------------------------------------ broadcasting
    # Upper bound on WebSocket pushes per second. The real rate follows the
    # upstream pipeline; this only stops a fast source from flooding clients.
    BROADCAST_MAX_FPS: float = 15.0

    # ------------------------------------------------------------ helpers
    @property
    def allowed_origins(self) -> list[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]

    @property
    def required_ppe(self) -> list[str]:
        return [p.strip().lower() for p in self.REQUIRED_PPE.split(",") if p.strip()]

    def resolve(self, relative: str) -> Path:
        """Resolve a config path against backend/ so the app works from any cwd."""
        p = Path(relative)
        return p if p.is_absolute() else (BACKEND_ROOT / p)

    @property
    def homography_path(self) -> Path:
        return self.resolve(self.HOMOGRAPHY_FILE)

    @property
    def site_plan_path(self) -> Path:
        return self.resolve(self.SITE_PLAN_FILE)

    @property
    def sqlalchemy_url(self) -> str:
        """Turn a relative sqlite path into an absolute one."""
        url = self.DATABASE_URL
        prefix = "sqlite:///"
        if url.startswith(prefix):
            raw = url[len(prefix) :]
            if raw.startswith("./"):
                raw = raw[2:]
            path = Path(raw)
            if not path.is_absolute():
                path = BACKEND_ROOT / path
            path.parent.mkdir(parents=True, exist_ok=True)
            return prefix + path.as_posix()
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
