from __future__ import annotations

from pathlib import Path
import os
import sys

import httpx


def load_dotenv() -> None:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / ".env"
        if candidate.exists():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())
            return


def main() -> int:
    load_dotenv()
    api_key = os.getenv("THEODDS_API_KEY", "").strip()
    if not api_key or api_key == "replace_with_theoddsapi_key":
        print("Missing THEODDS_API_KEY in .env")
        return 2

    base_url = os.getenv("THEODDS_API_BASE_URL", "https://api.the-odds-api.com/v4").rstrip("/")
    timeout_seconds = float(os.getenv("THEODDS_API_TIMEOUT_SECONDS", "20"))
    sport_keys = [
        item.strip()
        for item in os.getenv("THEODDS_API_SPORT_KEYS", "soccer_epl").split(",")
        if item.strip()
    ]
    regions = os.getenv("THEODDS_API_REGIONS", "eu,uk")
    markets = os.getenv("THEODDS_API_MARKETS", "h2h")

    with httpx.Client(timeout=timeout_seconds, headers={"Accept": "application/json"}) as client:
        sports_response = client.get(
            f"{base_url}/sports",
            params={"apiKey": api_key},
        )
        sports_response.raise_for_status()
        sports = sports_response.json()
        print(f"The Odds API /sports status: {sports_response.status_code}")
        print(f"The Odds API sports returned: {len(sports) if isinstance(sports, list) else 0}")

        sample_sport = sport_keys[0]
        odds_response = client.get(
            f"{base_url}/sports/{sample_sport}/odds",
            params={
                "apiKey": api_key,
                "regions": regions,
                "markets": markets,
                "oddsFormat": "decimal",
                "dateFormat": "iso",
            },
        )
        odds_response.raise_for_status()
        events = odds_response.json()
        print(f"The Odds API /sports/{sample_sport}/odds status: {odds_response.status_code}")
        print(f"The Odds API sample sport events returned: {len(events) if isinstance(events, list) else 0}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
