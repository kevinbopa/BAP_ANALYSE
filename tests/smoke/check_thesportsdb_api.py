from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def main() -> int:
    api_key = os.getenv("THESPORTSDB_API_KEY", "123").strip() or "123"
    base_url = os.getenv("THESPORTSDB_BASE_URL", "https://www.thesportsdb.com/api/v1/json").rstrip("/")
    endpoint = f"{base_url}/{api_key}/all_leagues.php"

    try:
        with urllib.request.urlopen(endpoint, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        print(f"API smoke test failed: {exc}")
        return 1

    leagues = payload.get("leagues") or []
    if not isinstance(leagues, list) or not leagues:
        print("API smoke test failed: no leagues returned")
        return 2

    print(f"API smoke test succeeded: {len(leagues)} leagues returned from {endpoint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
