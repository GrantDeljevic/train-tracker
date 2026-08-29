from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import httpx

from .config import settings

LOGGER = logging.getLogger(__name__)


class TomTomError(RuntimeError):
    pass


class TomTomConfigurationError(TomTomError):
    pass


class RequestBudgetExceeded(TomTomError):
    pass


@dataclass(frozen=True)
class TileKey:
    z: int
    x: int
    y: int
    flow_type: str = "relative"

    def as_string(self) -> str:
        return f"{self.z}/{self.x}/{self.y}/{self.flow_type}"


@dataclass
class TileResponse:
    key: TileKey
    body: bytes
    fetched_at: datetime
    from_cache: bool = False
    status_code: int = 200
    error: str | None = None


class TomTomClient:
    """Small provider adapter with a shared in-process freshness cache."""

    def __init__(
        self,
        api_key: str | None = None,
        endpoint: str | None = None,
        url_template: str | None = None,
        client: httpx.Client | None = None,
        timeout: float | None = None,
        cache_seconds: int | None = None,
        max_retries: int | None = None,
        request_guard: Callable[[], bool] | None = None,
        usage_callback: Callable[[int | None, str], None] | None = None,
    ):
        self.api_key = api_key if api_key is not None else settings.tomtom_api_key
        self.endpoint = (endpoint or settings.tomtom_endpoint).lower()
        self.url_template = url_template or settings.tomtom_url_template
        self.client = client or httpx.Client(timeout=timeout or settings.request_timeout)
        self._owns_client = client is None
        self.cache_seconds = cache_seconds if cache_seconds is not None else settings.tile_cache_seconds
        self.max_retries = max_retries if max_retries is not None else settings.max_retries
        self.request_guard = request_guard
        self.usage_callback = usage_callback
        self._cache: dict[TileKey, TileResponse] = {}
        self._cache_lock = threading.Lock()
        self._request_semaphore = threading.Semaphore(4)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def url_for(self, key: TileKey) -> tuple[str, dict[str, str]]:
        if self.url_template:
            url = self.url_template.format(z=key.z, x=key.x, y=key.y, zoom=key.z)
            return url, {"key": self.api_key or ""}
        if self.endpoint == "orbis":
            return (
                f"https://api.tomtom.com/maps/orbis/traffic/tile/flow/{key.z}/{key.x}/{key.y}.pbf",
                {"apiVersion": "1", "key": self.api_key or "", "tags": "road_category,road_subcategory,road_closure,relative_speed,absolute_speed"},
            )
        return (
            f"https://api.tomtom.com/traffic/map/4/tile/flow/{key.flow_type}/{key.z}/{key.x}/{key.y}.pbf",
                {"key": self.api_key or "", "tags": "[road_type,traffic_level,traffic_road_coverage,road_closure,road_category,road_subcategory]"},
        )

    def fetch_tile(self, key: TileKey, force: bool = False) -> TileResponse:
        if not self.api_key:
            raise TomTomConfigurationError("TOMTOM_API_KEY is not configured")
        now = datetime.now(timezone.utc)
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached and not force and (now - cached.fetched_at).total_seconds() < self.cache_seconds:
                cached.from_cache = True
                if self.usage_callback:
                    self.usage_callback(None, "cache")
                return cached
        url, params = self.url_for(key)
        last_error: str | None = None
        with self._request_semaphore:
            for attempt in range(self.max_retries + 1):
                if self.request_guard and not self.request_guard():
                    raise RequestBudgetExceeded("TomTom monthly hard request budget reached")
                try:
                    response = self.client.get(
                        url,
                        params=params,
                        headers={"Accept": "application/x-protobuf", "Accept-Encoding": "gzip"},
                    )
                    if self.usage_callback:
                        self.usage_callback(response.status_code, "http")
                    if response.status_code == 200:
                        result = TileResponse(key=key, body=response.content, fetched_at=now, status_code=200)
                        with self._cache_lock:
                            self._cache[key] = result
                        return result
                    if response.status_code == 429:
                        last_error = "TomTom rate limit (429)"
                    elif response.status_code >= 500:
                        last_error = f"TomTom server error ({response.status_code})"
                    else:
                        last_error = f"TomTom request failed ({response.status_code})"
                    if response.status_code < 500 and response.status_code != 429:
                        break
                except (httpx.HTTPError, OSError) as exc:
                    if self.usage_callback:
                        self.usage_callback(None, "network")
                    last_error = f"TomTom network error: {exc}"
                if attempt < self.max_retries:
                    retry_after = 0.0
                    try:
                        retry_after = min(float(response.headers.get("Retry-After", "0")), 10.0)
                    except (UnboundLocalError, ValueError):
                        pass
                    time.sleep(max(retry_after, min(2**attempt, 4)))
        raise TomTomError(last_error or "TomTom request failed")
