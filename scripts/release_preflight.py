from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Check:
    name: str
    command: list[str]


CHECKS: tuple[Check, ...] = (
    Check(
        "Compile Python sources",
        [sys.executable, "-m", "compileall", "-q", "apps", "scripts", "services", "tests"],
    ),
    Check(
        "Validate migration catalog",
        [sys.executable, "scripts/apply_migrations.py", "--validate-only"],
    ),
    Check(
        "Scan migration safety",
        [sys.executable, "scripts/check_migration_safety.py"],
    ),
    Check(
        "Plan centralized cycle",
        [sys.executable, "scripts/run_cycle.py", "--plan"],
    ),
    Check(
        "Run dashboard smoke tests",
        [sys.executable, "tests/smoke/check_dashboard_app.py"],
    ),
)


def run_check(check: Check) -> dict[str, object]:
    started_at = datetime.now(timezone.utc)
    result = subprocess.run(
        check.command,
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
    )
    finished_at = datetime.now(timezone.utc)
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    return {
        "name": check.name,
        "command": check.command,
        "status": "success" if result.returncode == 0 else "failed",
        "return_code": result.returncode,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "stdout_tail": stdout[-4000:] if stdout else "",
        "stderr_tail": stderr[-4000:] if stderr else "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run lightweight release guards before a production deployment."
    )
    parser.add_argument("--output", default="", help="Optional path for the JSON summary.")
    args = parser.parse_args()

    summary: dict[str, object] = {
        "status": "success",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "checks": [],
    }

    for check in CHECKS:
        print(f"[release] {check.name}...")
        result = run_check(check)
        summary["checks"].append(result)
        if result["status"] != "success":
            summary["status"] = "failed"
            print(f"[release] failed: {check.name}")
            break
        print(f"[release] ok: {check.name}")

    output = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output:
        output_path = Path(args.output).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output, encoding="utf-8")
    print(output)
    return 0 if summary["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
