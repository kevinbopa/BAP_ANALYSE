from __future__ import annotations

import time
from urllib.parse import urlencode

import httpx

from spe_ingestion.config import DataGolfSettings


class DataGolfError(RuntimeError):
    """Raised when the DataGolf API returns an error payload."""


class DataGolfClient:
    """Client for the DataGolf feeds API (https://feeds.datagolf.com).

    DataGolf couvre le circuit masculin complet (PGA/DP World/Korn Ferry/LIV)
    avec le modele strokes-gained + course-fit deja calcule (pre-tournament),
    les cotes books (dont bet365) pour outrights et matchups, et le fair-odd
    maison. Limite : 45 requetes/minute -> throttle par min_interval_seconds.
    """

    def __init__(self, settings: DataGolfSettings) -> None:
        self._settings = settings
        self._client = httpx.Client(
            timeout=settings.timeout_seconds,
            headers={"Accept": "application/json"},
        )
        self._last_request = 0.0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DataGolfClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        wait = self._settings.min_interval_seconds - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _build_url(self, path: str, params: dict[str, str]) -> str:
        base = self._settings.base_url.rstrip("/")
        return f"{base}/{path.lstrip('/')}?{urlencode(params)}"

    def _get(self, path: str, params: dict[str, str] | None = None) -> list | dict:
        merged: dict[str, str] = dict(params or {})
        merged.setdefault("file_format", self._settings.file_format)
        merged["key"] = self._settings.api_key
        url = self._build_url(path, merged)

        last_error: Exception | None = None
        for _attempt in range(max(1, self._settings.max_retries)):
            self._throttle()
            try:
                response = self._client.get(url)
                if response.status_code == 429:
                    # Depassement de quota : DataGolf suspend 5 min, on patiente.
                    time.sleep(5.0)
                    continue
                if 400 <= response.status_code < 500:
                    # Tour/marche non offert (ex: pas d'event actif) : inutile
                    # de reessayer, on remonte proprement pour laisser l'appelant
                    # sauter ce tour/marche.
                    raise DataGolfError(
                        f"{response.status_code} {path}: {response.text[:120]}"
                    )
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, dict):
                    message = payload.get("message")
                    if message and len(payload) == 1:
                        raise DataGolfError(str(message))
                return payload
            except httpx.HTTPError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise DataGolfError(f"DataGolf request failed: {path}")

    # --- General use -----------------------------------------------------
    def get_schedule(self, tour: str = "all") -> dict:
        payload = self._get("get-schedule", {"tour": tour})
        return payload if isinstance(payload, dict) else {"schedule": payload}

    def get_field_updates(self, tour: str) -> dict:
        payload = self._get("field-updates", {"tour": tour})
        return payload if isinstance(payload, dict) else {}

    # --- Model predictions ----------------------------------------------
    def get_pre_tournament(self, tour: str) -> dict:
        # percent -> le modele renvoie des PROBABILITES (0-1), pas des cotes.
        payload = self._get(
            "preds/pre-tournament",
            {"tour": tour, "odds_format": "percent"},
        )
        return payload if isinstance(payload, dict) else {}

    def get_skill_ratings(self, display: str = "value") -> dict:
        payload = self._get("preds/skill-ratings", {"display": display})
        return payload if isinstance(payload, dict) else {}

    def get_in_play(self, tour: str) -> dict:
        """Positions courantes/finales (current_pos, make_cut) -> reglement."""
        payload = self._get("preds/in-play", {"tour": tour})
        return payload if isinstance(payload, dict) else {}

    # --- Betting tools ---------------------------------------------------
    def get_outrights(self, tour: str, market: str) -> dict:
        payload = self._get(
            "betting-tools/outrights",
            {"tour": tour, "market": market, "odds_format": self._settings.odds_format},
        )
        return payload if isinstance(payload, dict) else {}

    def get_matchups(self, tour: str, market: str = "tournament_matchups") -> dict:
        payload = self._get(
            "betting-tools/matchups",
            {"tour": tour, "market": market, "odds_format": self._settings.odds_format},
        )
        return payload if isinstance(payload, dict) else {}
