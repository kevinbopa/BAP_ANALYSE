from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Mapping


_LISTISH_KEYS = (
    "response",
    "events",
    "teams",
    "leagues",
    "seasons",
    "players",
    "player",
    "bookmakers",
    "odds",
    "match_list",
    "data",
    "fixtures",
    "categories",
    "tournaments",
    "lineup",
    "timeline",
    "eventstats",
)


def payload_entity_count(payload: Any) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, Mapping):
        for key in _LISTISH_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
        return len(payload)
    return 1 if payload not in (None, "") else 0


def store_provider_payload(
    cursor,
    *,
    provider_id: int,
    endpoint_id: int | None = None,
    ingestion_run_id: str | None = None,
    object_type: str,
    object_id: Any,
    natural_key: str,
    payload: Any,
    request_path: str | None = None,
    request_params: Mapping[str, Any] | None = None,
    source_url: str | None = None,
    http_status: int | None = None,
) -> int:
    payload_text = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    params_text = json.dumps(request_params or {}, sort_keys=True, ensure_ascii=True)
    checksum = sha256(payload_text.encode("utf-8")).hexdigest()
    cursor.execute(
        """
        INSERT INTO raw.provider_payloads (
            provider_id,
            endpoint_id,
            ingestion_run_id,
            provider_object_type,
            provider_object_id,
            natural_key,
            payload,
            payload_checksum,
            request_path,
            request_params,
            source_url,
            http_status,
            payload_size_bytes,
            entity_count
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s, %s, %s, %s)
        RETURNING provider_payload_id
        """,
        (
            provider_id,
            endpoint_id,
            ingestion_run_id,
            object_type,
            str(object_id) if object_id is not None else None,
            natural_key,
            payload_text,
            checksum,
            request_path,
            params_text,
            source_url,
            http_status,
            len(payload_text.encode("utf-8")),
            payload_entity_count(payload),
        ),
    )
    return int(cursor.fetchone()[0])


def record_payload_normalization(
    cursor,
    *,
    provider_payload_id: int,
    normalization_target: str,
    status_code: str,
    records_written: int = 0,
    ingestion_run_id: str | None = None,
    error_message: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    cursor.execute(
        """
        INSERT INTO ops.provider_payload_normalizations (
            provider_payload_id,
            ingestion_run_id,
            normalization_target,
            status_code,
            records_written,
            error_message,
            metadata,
            normalized_at,
            updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, now(), now())
        ON CONFLICT (provider_payload_id, normalization_target)
        DO UPDATE SET
            ingestion_run_id = EXCLUDED.ingestion_run_id,
            status_code = EXCLUDED.status_code,
            records_written = EXCLUDED.records_written,
            error_message = EXCLUDED.error_message,
            metadata = EXCLUDED.metadata,
            normalized_at = now(),
            updated_at = now()
        """,
        (
            provider_payload_id,
            ingestion_run_id,
            normalization_target,
            status_code,
            max(0, int(records_written)),
            error_message[:1000] if error_message else None,
            json.dumps(metadata or {}, sort_keys=True, ensure_ascii=True),
        ),
    )
