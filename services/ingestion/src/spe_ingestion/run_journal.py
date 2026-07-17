from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sys
from typing import Any, Mapping


ALLOWED_TRIGGER_SOURCES = {"MANUAL", "SCHEDULED", "BACKFILL", "RECOVERY", "UNKNOWN"}
APP_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def current_application_name() -> str:
    raw = (
        os.getenv("SPE_RUN_APPLICATION", "").strip()
        or Path(sys.argv[0] or "bp-edge").stem.strip()
        or "bp-edge"
    )
    sanitized = APP_NAME_SAFE_RE.sub("-", raw)
    return sanitized[:63] or "bp-edge"


def current_trigger_source(default: str = "MANUAL") -> str:
    value = (os.getenv("SPE_RUN_TRIGGER", "").strip().upper() or default.upper() or "MANUAL")
    return value if value in ALLOWED_TRIGGER_SOURCES else "UNKNOWN"


def current_host_name() -> str:
    return (socket.gethostname() or "local").strip()[:255] or "local"


def current_process_id() -> int:
    return int(os.getpid())


def request_payload_json(payload: Mapping[str, Any] | None = None) -> str:
    return json.dumps(payload or {}, sort_keys=True, separators=(",", ":"))


def request_fingerprint(payload_json: str) -> str:
    return hashlib.md5(payload_json.encode("utf-8")).hexdigest()


def run_metadata(payload: Mapping[str, Any] | None = None, *, default_trigger: str = "MANUAL") -> dict[str, Any]:
    payload_json = request_payload_json(payload)
    return {
        "request_params_json": payload_json,
        "request_fingerprint": request_fingerprint(payload_json),
        "application_name": current_application_name(),
        "trigger_source": current_trigger_source(default_trigger),
        "host_name": current_host_name(),
        "process_id": current_process_id(),
    }


def touch_run(cursor, run_id: str) -> None:
    cursor.execute(
        """
        UPDATE ops.ingestion_runs
        SET heartbeat_at = now()
        WHERE ingestion_run_id = %s
        """,
        (run_id,),
    )
