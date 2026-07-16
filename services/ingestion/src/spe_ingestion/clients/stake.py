from __future__ import annotations

from urllib.parse import urlencode

import httpx

from spe_ingestion.config import StakeSettings


class StakeClient:
    """HTTP client for Stake odds-data endpoints."""

    def __init__(self, settings: StakeSettings) -> None:
        self._settings = settings
        headers = {"Accept": "application/json"}
        if settings.api_token:
            auth_value = settings.api_token
            if settings.auth_scheme:
                auth_value = f"{settings.auth_scheme} {settings.api_token}"
            headers[settings.auth_header] = auth_value

        self._client = httpx.Client(
            timeout=settings.timeout_seconds,
            headers=headers,
        )

    def _build_url(self, path: str, params: dict[str, str] | None = None) -> str:
        base = self._settings.base_url.rstrip("/")
        url = f"{base}/{path.lstrip('/')}"
        if params:
            url = f"{url}?{urlencode(params)}"
        return url

    def _get_json(self, path: str, params: dict[str, str] | None = None) -> dict | list:
        response = self._client.get(self._build_url(path, params))
        response.raise_for_status()
        return response.json()

    def get_sports(self) -> list[dict]:
        payload = self._get_json(self._settings.sports_path)
        if isinstance(payload, list):
            return payload
        return []

    def get_categories(self, sport_slug: str) -> list[dict]:
        path = self._settings.categories_path_template.format(sport=sport_slug)
        payload = self._get_json(path)
        if isinstance(payload, dict):
            items = payload.get("categories") or payload.get("category") or []
            return [item for item in items if isinstance(item, dict)]
        return []

    def get_tournaments(self, sport_slug: str, category_slug: str) -> list[dict]:
        path = self._settings.tournaments_path_template.format(
            sport=sport_slug,
            category=category_slug,
        )
        payload = self._get_json(path)
        tournaments = _extract_dict_list(payload, "tournaments")
        if tournaments:
            return tournaments

        fallback_path = f"/sport/{sport_slug}/category/{category_slug}/tournament"
        payload = self._get_json(fallback_path)
        return _extract_dict_list(payload, "tournament")

    def get_fixtures(self, sport_slug: str, category_slug: str, tournament_slug: str) -> list[dict]:
        path = self._settings.fixtures_path_template.format(
            sport=sport_slug,
            category=category_slug,
            tournament=tournament_slug,
        )
        payload = self._get_json(path)
        fixtures = _extract_dict_list(payload, "fixtures")
        if fixtures:
            return fixtures

        fallback_path = (
            f"/sport/{sport_slug}/category/{category_slug}/tournament/{tournament_slug}/fixture"
        )
        payload = self._get_json(fallback_path)
        return _extract_dict_list(payload, "fixture")

    def get_fixture(self, fixture_slug: str) -> dict:
        path = self._settings.fixture_path_template.format(fixture_slug=fixture_slug)
        payload = self._get_json(path)
        if isinstance(payload, dict):
            return payload
        raise ValueError(f"Unexpected Stake fixture payload type for {fixture_slug}")

    def get_fixture_odds(self, fixture_slug: str) -> dict:
        path = self._settings.odds_path_template.format(fixture_slug=fixture_slug)
        response = self._client.get(self._build_url(path))
        if response.status_code == 404:
            return self.get_fixture(fixture_slug)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            return payload
        raise ValueError(f"Unexpected Stake odds payload type for {fixture_slug}")

    def close(self) -> None:
        self._client.close()


def _extract_dict_list(payload: dict | list, key: str) -> list[dict]:
    if isinstance(payload, dict):
        items = payload.get(key) or []
        return [item for item in items if isinstance(item, dict)]
    return []
