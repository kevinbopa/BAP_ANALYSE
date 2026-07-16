from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
import sys

sys.path.insert(0, str((Path(__file__).resolve().parent / "src").resolve()))

from spe_ingestion.main import ingest_theoddsapi_odds_data


def main() -> int:
    summary = ingest_theoddsapi_odds_data()
    print(json.dumps(asdict(summary), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
