from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import subprocess
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
PREDICTION_SRC = ROOT_DIR / "services" / "prediction" / "src"
if str(PREDICTION_SRC) not in sys.path:
    sys.path.insert(0, str(PREDICTION_SRC))
if str(ROOT_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "scripts"))

from postgres_cli import is_localhost, load_db_settings, resolve_pg_binary, resolve_setting


def default_output_path(dbname: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ROOT_DIR / "tmp" / "db-backups" / f"{dbname}_{stamp}.dump"


def build_command(
    mode: str,
    settings,
    docker_container: str | None,
    pg_dump_path: str | None,
) -> tuple[list[str], dict[str, str]]:
    env = {}
    if mode == "binary":
        if not pg_dump_path:
            raise RuntimeError("pg_dump is not available in PATH and no pg bin directory was configured.")
        env["PGPASSWORD"] = settings.password
        return (
            [
                pg_dump_path,
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                "--host",
                settings.host,
                "--port",
                str(settings.port),
                "--username",
                settings.user,
                "--dbname",
                settings.dbname,
            ],
            env,
        )
    if not docker_container:
        raise RuntimeError("Docker mode requires a container name.")
    return (
        [
            "docker",
            "exec",
            "-e",
            f"PGPASSWORD={settings.password}",
            "-i",
            docker_container,
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--host",
            settings.host,
            "--port",
            str(settings.port),
            "--username",
            settings.user,
            "--dbname",
            settings.dbname,
        ],
        env,
    )


def pick_mode(requested: str, host: str, pg_dump_path: str | None, docker_container: str | None) -> str:
    if requested in {"binary", "docker"}:
        return requested
    if pg_dump_path:
        return "binary"
    if docker_container and is_localhost(host):
        return "docker"
    raise RuntimeError(
        "No backup mechanism available. Install pg_dump or configure a local Docker PostgreSQL container."
    )


def write_sha256(path: Path) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a PostgreSQL custom-format backup.")
    parser.add_argument("--output", default="", help="Output .dump path. Defaults to tmp/db-backups/<db>_<utc>.dump")
    parser.add_argument("--prefix", default="POSTGRES_MIGRATE", help="Primary env prefix for DB credentials.")
    parser.add_argument("--fallback-prefix", default="POSTGRES", help="Fallback env prefix for DB credentials.")
    parser.add_argument("--mode", choices=("auto", "binary", "docker"), default="auto")
    parser.add_argument("--docker-container", default="", help="Docker container name for local fallback.")
    args = parser.parse_args()

    settings = load_db_settings(args.prefix, args.fallback_prefix, admin_defaults=True)
    output_path = Path(args.output).resolve() if args.output else default_output_path(settings.dbname)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    pg_bin_dir = resolve_setting("PG_BIN_DIR", args.prefix, args.fallback_prefix, "")
    docker_container = args.docker_container or resolve_setting(
        "DOCKER_CONTAINER", args.prefix, args.fallback_prefix, "spe-postgres"
    )
    pg_dump_path = resolve_pg_binary("pg_dump", pg_bin_dir)
    mode = pick_mode(args.mode, settings.host, pg_dump_path, docker_container)
    command, extra_env = build_command(mode, settings, docker_container, pg_dump_path)

    env = None
    if extra_env:
        env = dict(os.environ)
        env.update(extra_env)

    with output_path.open("wb") as handle:
        result = subprocess.run(command, stdout=handle, stderr=subprocess.PIPE, env=env)
    if result.returncode != 0:
        if output_path.exists():
            output_path.unlink()
        raise SystemExit(result.stderr.decode("utf-8", errors="replace").strip() or "Backup failed.")

    write_sha256(output_path)
    print(f"Backup created: {output_path}")
    print(f"Checksum file: {output_path.with_suffix(output_path.suffix + '.sha256')}")
    print(f"Mode: {mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
