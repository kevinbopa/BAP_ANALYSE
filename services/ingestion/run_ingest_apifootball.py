"""Run the API-Football player-layer ingestion.

Usage:
    py services/ingestion/run_ingest_apifootball.py            # squads + injuries
    py services/ingestion/run_ingest_apifootball.py --stats    # + per-match player stats

Requires APIFOOTBALL_KEY in .env (free tier: https://www.api-football.com/).
Without the key the script exits cleanly with an explanatory message.
"""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sys

sys.path.insert(0, str((Path(__file__).resolve().parent / "src").resolve()))

from spe_ingestion.apifootball_ingestor import ApiFootballIngestor
from spe_ingestion.clients.apifootball import ApiFootballClient
from spe_ingestion.config import ApiFootballSettings
from spe_ingestion.db import DatabaseSettings, connect_db


def main() -> int:
    settings = ApiFootballSettings.from_env()
    if not settings.enabled:
        print(json.dumps({
            "status": "SKIPPED",
            "reason": "APIFOOTBALL_KEY absent du .env - cree un compte gratuit sur https://www.api-football.com/",
        }, indent=2))
        return 0

    with_stats = "--stats" in sys.argv
    client = ApiFootballClient(settings)
    connection = connect_db(DatabaseSettings.from_env(prefix="POSTGRES_INGEST"))
    try:
        ingestor = ApiFootballIngestor(client, settings)
        summary = ingestor.ingest_player_layer(connection, with_match_stats=with_stats)
        print(json.dumps(asdict(summary), indent=2))
        return 0
    finally:
        connection.close()
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
