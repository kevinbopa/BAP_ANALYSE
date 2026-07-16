"""Backtest walk-forward du moteur analytique.

    py services/prediction/run_backtest.py                 # evalue 2024-01-01 -> maintenant
    py services/prediction/run_backtest.py --from 2022-01-01
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str((Path(__file__).resolve().parent / "src").resolve()))

from spe_prediction.backtest import run_backtest
from spe_prediction.db import DatabaseSettings, connect_db


def main() -> int:
    args = sys.argv[1:]
    evaluate_from = datetime(2024, 1, 1, tzinfo=timezone.utc)
    if "--from" in args:
        evaluate_from = datetime.fromisoformat(args[args.index("--from") + 1]).replace(tzinfo=timezone.utc)

    connection = connect_db(DatabaseSettings.from_env())
    try:
        report = run_backtest(connection, evaluate_from)
    finally:
        connection.close()

    print(json.dumps(report, indent=2, ensure_ascii=False))
    out = Path(__file__).resolve().parent / "reports"
    out.mkdir(exist_ok=True)
    (out / "backtest_latest.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
