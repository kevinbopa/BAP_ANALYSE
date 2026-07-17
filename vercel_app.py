from __future__ import annotations

from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parent
DASHBOARD_DIR = ROOT_DIR / "apps" / "dashboard"

if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

from app import application as app  # noqa: E402


application = app
