from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    database_url: str
    tomtom_api_key: str | None
    tomtom_endpoint: str
    tomtom_url_template: str | None
    monthly_request_budget: int
    soft_request_budget: int
    fra_refresh_days: int
    enable_poller: bool
    auto_create_schema: bool
    host: str
    port: int
    timezone: str
    request_timeout: float
    max_retries: int
    tile_cache_seconds: int
    log_level: str
    detector_normal_threshold: float
    detector_weak_threshold: float
    detector_moderate_threshold: float
    detector_strong_threshold: float
    detector_drop_threshold: float


def load_settings() -> Settings:
    database_url = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'train_tracker.db'}")
    if database_url.startswith("postgres://"):
        database_url = "postgresql+psycopg://" + database_url[len("postgres://") :]
    elif database_url.startswith("postgresql://"):
        database_url = "postgresql+psycopg://" + database_url[len("postgresql://") :]
    return Settings(
        database_url=database_url,
        tomtom_api_key=os.getenv("TOMTOM_API_KEY") or None,
        tomtom_endpoint=os.getenv("TOMTOM_FLOW_ENDPOINT", "v4").strip().lower(),
        tomtom_url_template=os.getenv("TOMTOM_FLOW_URL_TEMPLATE") or None,
        monthly_request_budget=_int("TOMTOM_MONTHLY_REQUEST_BUDGET", 190_000),
        soft_request_budget=_int("TOMTOM_SOFT_REQUEST_BUDGET", 175_000),
        fra_refresh_days=_int("FRA_REFRESH_DAYS", 7),
        enable_poller=_bool("ENABLE_POLLER", True),
        auto_create_schema=_bool("AUTO_CREATE_SCHEMA", True),
        host=os.getenv("HOST", "0.0.0.0"),
        port=_int("PORT", 8000),
        timezone=os.getenv("TZ", "America/Detroit"),
        request_timeout=_float("HTTP_TIMEOUT_SECONDS", 15.0),
        max_retries=_int("HTTP_MAX_RETRIES", 2),
        tile_cache_seconds=_int("TILE_CACHE_SECONDS", 55),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        detector_normal_threshold=_float("TRAFFIC_NORMAL_THRESHOLD", 0.80),
        detector_weak_threshold=_float("TRAFFIC_WEAK_THRESHOLD", 0.75),
        detector_moderate_threshold=_float("TRAFFIC_MODERATE_THRESHOLD", 0.60),
        detector_strong_threshold=_float("TRAFFIC_STRONG_THRESHOLD", 0.35),
        detector_drop_threshold=_float("TRAFFIC_DROP_THRESHOLD", 0.20),
    )


settings = load_settings()

