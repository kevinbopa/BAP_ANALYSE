from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "scripts"))

from postgres_cli import load_db_settings


LOCK_KEY = 2_026_071_701


@dataclass(frozen=True)
class CycleStep:
    name: str
    command: list[str]
    critical: bool = True


DAILY_STEPS: tuple[CycleStep, ...] = (
    CycleStep("Sync scores TheSportsDB", ["services/ingestion/run_ingest_thesportsdb.py"]),
    CycleStep("Reglement des deals termines", ["services/prediction/run_settle_deals.py"]),
    CycleStep("Sync cotes The Odds API", ["services/ingestion/run_ingest_theoddsapi_odds.py"]),
    CycleStep("Meteo previsionnelle Open-Meteo", ["services/ingestion/run_ingest_weather.py"]),
    CycleStep(
        "Couche joueurs API-Football (squads + blessures + stats/match)",
        ["services/ingestion/run_ingest_apifootball.py", "--stats"],
    ),
    CycleStep(
        "Backfill xG stats de match (grandes ligues, incrementiel)",
        ["services/ingestion/run_ingest_apifootball_stats.py"],
    ),
    CycleStep("Scan de correlations", ["services/prediction/run_correlation_scan.py"]),
    CycleStep("Pipeline de predictions", ["services/prediction/run_prediction_pipeline.py"]),
    CycleStep("Cotes outright (vainqueurs)", ["services/ingestion/run_ingest_outright_odds.py"]),
    CycleStep("Faits divers (simulations long terme)", ["services/prediction/run_outright_predictions.py"]),
    CycleStep("Cotes buteur (anytime scorer)", ["services/ingestion/run_ingest_scorer_odds.py"]),
    CycleStep("Predictions buteur + deals", ["services/prediction/run_scorer_predictions.py"]),
    CycleStep(
        "Golf DataGolf (tournois/joueurs/SG/cotes/matchups/resultats)",
        ["services/ingestion/run_ingest_datagolf.py"],
    ),
    CycleStep("Reglement des deals golf (tournois termines)", ["services/prediction/run_settle_golf.py"]),
    CycleStep("Predictions golf + deals (modele DataGolf)", ["services/prediction/run_golf_predictions.py"]),
)


def append_log(log_file: Path | None, message: str) -> None:
    if log_file is None:
        return
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(message.rstrip() + "\n")


def connect_db():
    settings = load_db_settings("POSTGRES", "POSTGRES")
    import psycopg

    return psycopg.connect(settings.dsn())


def try_acquire_lock(connection) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,))
        return bool((cursor.fetchone() or [False])[0])


def release_lock(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    connection.commit()


def close_stale_ingestion_runs(connection) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE ops.ingestion_runs
            SET status_code = 'FAILED',
                finished_at = now(),
                heartbeat_at = now(),
                error_message = COALESCE(
                    error_message,
                    'Auto-closed by cycle runner: stale RUNNING ingestion'
                )
            WHERE status_code = 'RUNNING'
              AND COALESCE(heartbeat_at, started_at, requested_at) < now() - interval '2 hours'
            RETURNING ingestion_run_id::text
            """
        )
        stale = [str(row[0]) for row in cursor.fetchall()]
    connection.commit()
    return stale


def fresh_ingestion_running(connection) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM ops.ingestion_runs
            WHERE status_code = 'RUNNING'
              AND COALESCE(heartbeat_at, started_at, requested_at) >= now() - interval '2 hours'
            """
        )
        return int((cursor.fetchone() or [0])[0] or 0) > 0


def parse_step_payload(stdout: str) -> dict[str, object]:
    cleaned = stdout.strip()
    if not cleaned:
        return {}
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return {"stdout": cleaned[-4000:]}
    return parsed if isinstance(parsed, dict) else {"payload": parsed}


def execute_step(step: CycleStep, env: dict[str, str], log_file: Path | None) -> dict[str, object]:
    started = time.time()
    command = [sys.executable, *step.command]
    append_log(log_file, f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} | {step.name} ===")
    append_log(log_file, "CMD: " + " ".join(command))
    result = subprocess.run(
        command,
        cwd=str(ROOT_DIR),
        env=env,
        capture_output=True,
        text=True,
    )
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    if stdout:
        append_log(log_file, stdout)
    if stderr:
        append_log(log_file, stderr)
    duration = round(time.time() - started, 3)
    return {
        "name": step.name,
        "command": step.command,
        "critical": step.critical,
        "status": "success" if result.returncode == 0 else "failed",
        "return_code": result.returncode,
        "duration_seconds": duration,
        "payload": parse_step_payload(stdout),
        "stderr_tail": stderr[-4000:] if stderr else "",
    }


def build_steps(skip_score_sync: bool) -> list[CycleStep]:
    if not skip_score_sync:
        return list(DAILY_STEPS)
    return [step for step in DAILY_STEPS if step.name != "Sync scores TheSportsDB"]


def write_output(path: Path | None, summary: dict[str, object]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the BP//EDGE daily ingestion/prediction cycle.")
    parser.add_argument("--profile", default="daily", choices=("daily",), help="Cycle profile to execute.")
    parser.add_argument("--trigger", default="SCHEDULED", help="Run trigger source for observability.")
    parser.add_argument("--output", default="", help="Optional JSON summary output path.")
    parser.add_argument("--log-file", default="", help="Optional text log file path.")
    parser.add_argument("--plan", action="store_true", help="Print the selected steps without executing them.")
    args = parser.parse_args()

    log_file = Path(args.log_file).resolve() if args.log_file else None
    output_file = Path(args.output).resolve() if args.output else None

    if args.plan:
        summary = {
            "status": "planned",
            "profile": args.profile,
            "steps": [asdict(step) for step in DAILY_STEPS],
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        write_output(output_file, summary)
        return 0

    env = os.environ.copy()
    env["SPE_RUN_TRIGGER"] = args.trigger.strip().upper() or "SCHEDULED"
    env["SPE_RUN_APPLICATION"] = f"spe-cycle-{args.profile}"
    env["SPE_RUN_PROFILE"] = args.profile

    connection = connect_db()
    lock_acquired = False
    try:
        lock_acquired = try_acquire_lock(connection)
        if not lock_acquired:
            summary = {
                "status": "busy",
                "profile": args.profile,
                "message": "Un autre cycle est deja en cours sur cette base.",
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            write_output(output_file, summary)
            return 0

        stale_runs = close_stale_ingestion_runs(connection)
        skip_score_sync = fresh_ingestion_running(connection)
        steps = build_steps(skip_score_sync)
        append_log(log_file, f"Cycle profile={args.profile} trigger={env['SPE_RUN_TRIGGER']}")
        if stale_runs:
            append_log(log_file, "Stale ingestion runs closed: " + ", ".join(stale_runs))
        if skip_score_sync:
            append_log(log_file, "Score sync skipped because a fresh ingestion run is already active.")

        step_results: list[dict[str, object]] = []
        cycle_started = time.time()
        cycle_status = "success"
        for step in steps:
            result = execute_step(step, env, log_file)
            step_results.append(result)
            if result["status"] != "success" and step.critical:
                cycle_status = "failed"
                append_log(log_file, f"Cycle aborted on critical step: {step.name}")
                break

        summary = {
            "status": cycle_status,
            "profile": args.profile,
            "trigger": env["SPE_RUN_TRIGGER"],
            "application": env["SPE_RUN_APPLICATION"],
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "duration_seconds": round(time.time() - cycle_started, 3),
            "stale_runs_closed": stale_runs,
            "score_sync_skipped": skip_score_sync,
            "steps": step_results,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        write_output(output_file, summary)
        return 0 if cycle_status == "success" else 1
    finally:
        if lock_acquired:
            release_lock(connection)
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
