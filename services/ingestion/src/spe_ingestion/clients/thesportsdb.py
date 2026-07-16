from __future__ import annotations

import time
from urllib.parse import urlencode

import httpx

from spe_ingestion.config import TheSportsDBSettings


class TheSportsDBClient:
    def __init__(self, settings: TheSportsDBSettings) -> None:
        self._settings = settings
        self._client = httpx.Client(timeout=settings.timeout_seconds)
        self._last_request_monotonic = 0.0

    def _build_url(self, endpoint: str, params: dict[str, str] | None = None) -> str:
        base = self._settings.base_url.rstrip("/")
        url = f"{base}/{self._settings.api_key}/{endpoint.lstrip('/')}"
        if params:
            url = f"{url}?{urlencode(params)}"
        return url

    def _wait_for_rate_limit(self) -> None:
        now = time.monotonic()
        remaining = self._settings.min_interval_seconds - (now - self._last_request_monotonic)
        if remaining > 0:
            time.sleep(remaining)

    def get_json(self, endpoint: str, params: dict[str, str] | None = None) -> dict:
        url = self._build_url(endpoint, params)
        for attempt in range(self._settings.max_retries + 1):
            self._wait_for_rate_limit()
            response = self._client.get(url)
            self._last_request_monotonic = time.monotonic()
            if response.status_code != 429:
                response.raise_for_status()
                return response.json()

            retry_after = response.headers.get("Retry-After")
            if retry_after:
                sleep_seconds = max(self._settings.min_interval_seconds, float(retry_after))
            else:
                sleep_seconds = self._settings.min_interval_seconds * (attempt + 2)
            time.sleep(sleep_seconds)

        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self._client.close()
