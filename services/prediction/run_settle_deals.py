"""Regle les value bets dont les matchs sont termines + ROI cumule.

    py services/prediction/run_settle_deals.py

A lancer apres chaque sync de scores (le cycle quotidien ideal :
sync TheSportsDB -> settle -> sync cotes -> predictions).
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str((Path(__file__).resolve().parent / "src").resolve()))

from spe_prediction.db import DatabaseSettings, connect_db
from spe_prediction.settlement import settle_pending_bets


def main() -> int:
    connection = connect_db(DatabaseSettings.from_env())
    try:
        report = settle_pending_bets(connection)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
