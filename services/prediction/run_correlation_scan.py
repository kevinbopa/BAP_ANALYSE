"""Scan de correlations sur tout l'historique.

    py services/prediction/run_correlation_scan.py

A relancer apres chaque gros sync de donnees joueurs (le backfill enrichit
les compos -> les correlations joueur gagnent en profondeur).
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str((Path(__file__).resolve().parent / "src").resolve()))

from spe_prediction.correlations import CorrelationScanner
from spe_prediction.db import DatabaseSettings, connect_db


def main() -> int:
    connection = connect_db(DatabaseSettings.from_env())
    try:
        result = CorrelationScanner(connection).scan_all()
        print(json.dumps(result, indent=2))
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
