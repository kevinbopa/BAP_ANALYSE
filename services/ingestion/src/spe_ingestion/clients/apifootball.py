"""HTTP client for API-Football v3 (api-sports.io).

Player-layer data source: squads, injuries, lineups, per-match player stats.
Free tier: 100 requests/day, ~10 requests/minute — the client enforces a
minimum interval between calls and the ingestor keeps its request budget
explicit so a daily run never burns the quota blindly.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from spe_ingestion.config import ApiFootballSettings


class ApiFootballClient:
    def __init__(self, settings: ApiFootballSettings) -> None:
        self._settings = settings
        self._client = httpx.Client(
            base_url=settings.base_url,
            headers={"x-apisports-key": settings.api_key},
            timeout=settings.timeout_seconds,
        )
        self._last_request_at = 0.0
        self.requests_made = 0

    def close(self) -> None:
        self._client.close()

    def _wait_for_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        remaining = self._settings.min_interval_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._wait_for_rate_limit()
        response = self._client.get(path, params=params or {})
        self._last_request_at = time.monotonic()
        self.requests_made += 1
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"Unexpected API-Football payload for {path}")
        errors = payload.get("errors")
        # API-Football renvoie 200 avec un objet "errors" non vide en cas de
        # probleme (quota, cle invalide) — on le remonte explicitement.
        if errors and (isinstance(errors, dict) and any(errors.values()) or isinstance(errors, list) and errors):
            raise RuntimeError(f"API-Football error on {path}: {errors}")
        return payload

    def get_squad(self, team_apifootball_id: int) -> dict[str, Any]:
        return self.get_json("/players/squads", {"team": team_apifootball_id})

    def get_injuries(self, team_apifootball_id: int, season: int) -> dict[str, Any]:
        return self.get_json("/injuries", {"team": team_apifootball_id, "season": season})

    def get_fixture_lineups(self, fixture_apifootball_id: int) -> dict[str, Any]:
        return self.get_json("/fixtures/lineups", {"fixture": fixture_apifootball_id})

    def get_fixture_players(self, fixture_apifootball_id: int) -> dict[str, Any]:
        return self.get_json("/fixtures/players", {"fixture": fixture_apifootball_id})

    def get_team_fixtures(self, team_apifootball_id: int, season: int, last: int = 10) -> dict[str, Any]:
        return self.get_json(
            "/fixtures",
            {"team": team_apifootball_id, "season": season, "last": last},
        )

    def get_league_fixtures(self, league_apifootball_id: int, season: int) -> dict[str, Any]:
        """Tous les matchs d'une ligue-saison (pour backfill stats/xG)."""
        return self.get_json(
            "/fixtures",
            {"league": league_apifootball_id, "season": season},
        )

    def get_fixture_statistics(self, fixture_apifootball_id: int) -> dict[str, Any]:
        """Stats d'equipe d'un match : xG, tirs, corners, cartons, possession."""
        return self.get_json("/fixtures/statistics", {"fixture": fixture_apifootball_id})
