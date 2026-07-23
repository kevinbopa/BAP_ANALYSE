"""Run a dashboard action in a detached subprocess.

Usage:
    python run_action.py <action> <output_json> [--league NAME]
                         [--golf-tournament ID] [--golf-filters JSON]

Writes the ActionReport as JSON to <output_json> and removes the companion
<output_json>.running marker file when done.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_DIR = Path(__file__).resolve().parent

for p in (
    str(DASHBOARD_DIR),
    str(ROOT / "services" / "ingestion" / "src"),
    str(ROOT / "services" / "prediction" / "src"),
):
    if p not in sys.path:
        sys.path.insert(0, p)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action")
    parser.add_argument("output")
    parser.add_argument("--league", default=None)
    parser.add_argument("--golf-tournament", default="all")
    parser.add_argument("--golf-filters", default=None)
    args = parser.parse_args()

    output_file = Path(args.output)
    running_file = output_file.with_suffix(".running")

    golf_filters = None
    if args.golf_filters:
        try:
            golf_filters = json.loads(args.golf_filters)
        except json.JSONDecodeError:
            pass

    try:
        from app import action_label, action_progress_payload, execute_action

        def progress_callback(
            current_step: int,
            total_steps: int,
            step_label: str,
            detail: dict[str, object] | None = None,
        ) -> None:
            output_file.write_text(
                json.dumps(
                    {
                        "status": "running",
                        "title": f"Mise a jour en cours : {action_label(args.action)}",
                        "worker_pid": os.getpid(),
                        **action_progress_payload(
                            args.action,
                            current_step=current_step,
                            total_steps=total_steps,
                            step_label=step_label,
                            detail=detail,
                            info="Le site reste utilisable pendant la mise a jour.",
                        ),
                    },
                    ensure_ascii=False,
                    default=str,
                ),
                encoding="utf-8",
            )
            running_file.touch()

        report = execute_action(
            args.action,
            league_name=args.league,
            golf_tournament=args.golf_tournament,
            golf_filters=golf_filters,
            progress_callback=progress_callback,
        )
        result = {
            "status": "done",
            "title": report.title,
            "report_status": report.status,
            "worker_pid": os.getpid(),
            "payload": report.payload,
        }
    except Exception as exc:
        result = {
            "status": "failed",
            "worker_pid": os.getpid(),
            "error": f"{exc}\n\n{traceback.format_exc()}",
        }

    output_file.write_text(
        json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8"
    )
    try:
        running_file.unlink(missing_ok=True)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
