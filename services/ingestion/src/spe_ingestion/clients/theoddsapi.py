from __future__ import annotations

from datetime import datetime
from urllib.parse import urlencode

import httpx

from spe_ingestion.config import TheOddsApiSettings


class TheOddsApiClient:
    """Client for The Odds API v4 current odds endpoints."""

    def __init__(self, settings: TheOddsApiSettings) -> None:
        self._settings = settings
        self._client = httpx.Client(
            timeout=settings.timeout_seconds,
            headers={"Accept": "application/json"},
        )

    def _build_url(self, path: str, params: dict[str, str] | None = None) -> str:
        base = self._settings.base_url.rstrip("/")
        url = f"{base}/{path.lstrip('/')}"
        if params:
            url = f"{url}?{urlencode(params)}"
        return url

    def _get_json(self, path: str, params: dict[str, str] | None = None) -> list | dict:
        response = self._client.get(self._build_url(path, params))
        response.raise_for_status()
        return response.json()

    def get_sports(self) -> list[dict]:
        payload = self._get_json(
            "/sports",
            {"apiKey": self._settings.api_key},
        )
        if isinstance(payload, list):
            return payload
        return []

    def get_odds(
        self,
        sport_key: str,
        *,
        commence_from: datetime | None = None,
        commence_to: datetime | None = None,
    ) -> list[dict]:
        params = {
            "apiKey": self._settings.api_key,
            "regions": self._settings.regions,
            "markets": self._settings.markets,
            "oddsFormat": self._settings.odds_format,
            "dateFormat": self._settings.date_format,
        }
        if self._settings.bookmakers:
            params["bookmakers"] = self._settings.bookmakers
        if commence_from is not None:
            params["commenceTimeFrom"] = _format_api_datetime(commence_from)
        if commence_to is not None:
            params["commenceTimeTo"] = _format_api_datetime(commence_to)

        payload = self._get_json(f"/sports/{sport_key}/odds", params)
        if isinstance(payload, list):
            return payload
        return []

    def get_event_odds(self, sport_key: str, event_id: str, markets: str,
                       regions: str | None = None) -> dict:
        """Cotes d'UN evenement — seul endpoint qui sert les marches
        additionnels (btts, handicap, etc.) absents du bulk /odds.

        Cout quota : 1 x regions x markets par appel — a reserver aux
        evenements proches. `regions` permet de limiter (ex 'eu') pour
        les marches additionnels multiples."""
        params = {
            "apiKey": self._settings.api_key,
            "regions": regions or self._settings.regions,
            "markets": markets,
            "oddsFormat": self._settings.odds_format,
            "dateFormat": self._settings.date_format,
        }
        if self._settings.bookmakers:
            params["bookmakers"] = self._settings.bookmakers
        payload = self._get_json(f"/sports/{sport_key}/events/{event_id}/odds", params)
        if isinstance(payload, dict):
            return payload
        return {}

    def get_events(self, sport_key: str) -> list[dict]:
        """Events for a sport key. Useful for golf tournament discovery.

        The Odds API does not charge quota for the sports catalogue, but event
        and odds endpoints can consume credits; callers should keep this scoped.
        """
        payload = self._get_json(
            f"/sports/{sport_key}/events",
            {
                "apiKey": self._settings.api_key,
                "dateFormat": self._settings.date_format,
            },
        )
        if isinstance(payload, list):
            return payload
        return []

    def get_event_markets(self, sport_key: str, event_id: str) -> list[dict]:
        """Available markets for one event when the provider exposes them."""
        payload = self._get_json(
            f"/sports/{sport_key}/events/{event_id}/markets",
            {"apiKey": self._settings.api_key},
        )
        if isinstance(payload, list):
            return payload
        return []

    def get_participants(self, sport_key: str) -> list[dict]:
        """Sport participants, when exposed by The Odds API."""
        payload = self._get_json(
            f"/sports/{sport_key}/participants",
            {"apiKey": self._settings.api_key},
        )
        if isinstance(payload, list):
            return payload
        return []

    def get_outrights(
        self,
        sport_key: str,
        *,
        markets: str = "outrights",
        bookmakers: str | None = None,
    ) -> list[dict]:
        """Cotes outright (vainqueur de competition) d'un sport dedie
        (ex: soccer_fifa_world_cup_winner). Cout : 1 x regions credits."""
        params = {
            "apiKey": self._settings.api_key,
            "regions": self._settings.regions,
            "markets": markets,
            "oddsFormat": self._settings.odds_format,
            "dateFormat": self._settings.date_format,
        }
        selected_bookmakers = bookmakers if bookmakers is not None else self._settings.bookmakers
        if selected_bookmakers:
            params["bookmakers"] = selected_bookmakers
        payload = self._get_json(f"/sports/{sport_key}/odds", params)
        if isinstance(payload, list):
            return payload
        return []

    def close(self) -> None:
        self._client.close()


def _format_api_datetime(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")
