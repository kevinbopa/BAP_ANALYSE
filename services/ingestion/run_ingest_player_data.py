"""Ingestion des donnees joueurs TheSportsDB Premium.

Usage:
    py services/ingestion/run_ingest_player_data.py --rosters
        # effectifs complets de toutes les equipes (~20 min)

    py services/ingestion/run_ingest_player_data.py --backfill
        # lineups + timeline + stats des fixtures termines (internationaux
        # et recents d'abord). Interruptible: relancer reprend ou c'etait.

    py services/ingestion/run_ingest_player_data.py --backfill --limit 500
    py services/ingestion/run_ingest_player_data.py --backfill --international-only

    py services/ingestion/run_ingest_player_data.py --context
        # fiche evenement complete : meteo constatee, affluence, ville/pays
        # du stade (matchs futurs d'abord). Reprenable.
"""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sys

sys.path.insert(0, str((Path(__file__).resolve().parent / "src").resolve()))

from spe_ingestion.clients.thesportsdb import TheSportsDBClient
from spe_ingestion.config import TheSportsDBSettings
from spe_ingestion.db import DatabaseSettings, connect_db
from spe_ingestion.thesportsdb_player_ingestor import TheSportsDBPlayerIngestor


def main() -> int:
    args = sys.argv[1:]
    do_rosters = "--rosters" in args
    do_backfill = "--backfill" in args
    do_context = "--context" in args
    if not do_rosters and not do_backfill and not do_context:
        print(__doc__)
        return 2

    limit = None
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])
    international_only = "--international-only" in args

    settings = TheSportsDBSettings.from_env()
    client = TheSportsDBClient(settings)
    connection = connect_db(DatabaseSettings.from_env(prefix="POSTGRES_INGEST"))
    try:
        ingestor = TheSportsDBPlayerIngestor(client)
        results: dict = {}
        if do_rosters:
            results["rosters"] = asdict(ingestor.ingest_team_rosters(connection))
        if do_backfill:
            results["backfill"] = asdict(
                ingestor.backfill_fixture_details(
                    connection, limit=limit, international_only=international_only
                )
            )
        if do_context:
            results["context"] = asdict(
                ingestor.backfill_event_context(connection, limit=limit)
            )
        print(json.dumps(results, indent=2))
        return 0
    finally:
        connection.close()
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
