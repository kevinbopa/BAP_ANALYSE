"""Meteo previsionnelle des tournois golf (Open-Meteo, gratuit).

    py services/ingestion/run_ingest_golf_weather.py

Le script utilise les champs venue_* de core.golf_tournaments. Si l'API odds ne
fournit pas le parcours, on peut ajouter un mapping optionnel dans .env :

GOLF_TOURNAMENT_VENUES_JSON={"Masters Tournament":{"city":"Augusta","country":"United States"}}
"""
from __future__ import annotations

from datetime import timezone
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str((Path(__file__).resolve().parent / "src").resolve()))

from spe_ingestion.config import TheOddsApiSettings  # declenche le .env
from spe_ingestion.db import DatabaseSettings, connect_db
from spe_ingestion.openmeteo import OpenMeteoClient, weather_factor

TheOddsApiSettings.from_env()


def _venue_mapping() -> dict[str, dict]:
    raw = os.getenv("GOLF_TOURNAMENT_VENUES_JSON", "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def main() -> int:
    client = OpenMeteoClient()
    mapping = _venue_mapping()
    connection = connect_db(DatabaseSettings.from_env(prefix="POSTGRES_INGEST"))
    summary = {
        "tournois_examines": 0,
        "meteo_ecrite": 0,
        "lieu_introuvable": 0,
        "prevision_indisponible": 0,
        "mapping_env": len(mapping),
    }
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT golf_tournament_id, tournament_name, commence_time,
                       venue_city, venue_country, venue_lat, venue_lon
                FROM core.golf_tournaments
                WHERE completed_at IS NULL
                  AND commence_time >= now()
                  AND commence_time <= now() + interval '16 days'
                ORDER BY commence_time
                """
            )
            rows = cursor.fetchall()
            for tournament_id, name, commence_time, city, country, lat, lon in rows:
                summary["tournois_examines"] += 1
                mapped = mapping.get(str(name), {}) if isinstance(mapping.get(str(name)), dict) else {}
                city = city or mapped.get("city")
                country = country or mapped.get("country")
                lat = lat or mapped.get("lat") or mapped.get("latitude")
                lon = lon or mapped.get("lon") or mapped.get("longitude")

                coords = None
                if lat is not None and lon is not None:
                    coords = (float(lat), float(lon))
                elif city:
                    coords = client.geocode(str(city), str(country) if country else None)
                if coords is None:
                    summary["lieu_introuvable"] += 1
                    continue

                kickoff = commence_time if commence_time.tzinfo else commence_time.replace(tzinfo=timezone.utc)
                conditions = client.forecast_at(coords[0], coords[1], kickoff)
                if conditions is None:
                    summary["prevision_indisponible"] += 1
                    continue
                value, note = weather_factor(
                    conditions["temperature_c"],
                    conditions["precipitation_mm"],
                    conditions["wind_speed_kmh"],
                )
                cursor.execute(
                    """
                    INSERT INTO core.golf_tournament_context_factors (
                        golf_tournament_id, factor_code, factor_value, weight,
                        source_code, note, raw_context_json
                    )
                    VALUES (%s, 'WEATHER', %s, 1.0, 'OPEN_METEO', %s, %s::jsonb)
                    ON CONFLICT (golf_tournament_id, factor_code, source_code) DO UPDATE
                    SET factor_value = EXCLUDED.factor_value,
                        note = EXCLUDED.note,
                        raw_context_json = EXCLUDED.raw_context_json,
                        created_at = now()
                    """,
                    (
                        int(tournament_id),
                        value,
                        note,
                        json.dumps(
                            {
                                "coords": {"lat": coords[0], "lon": coords[1]},
                                "conditions": conditions,
                                "city": city,
                                "country": country,
                            },
                            ensure_ascii=True,
                        ),
                    ),
                )
                cursor.execute(
                    """
                    UPDATE core.golf_tournaments
                    SET venue_city = COALESCE(venue_city, %s),
                        venue_country = COALESCE(venue_country, %s),
                        venue_lat = COALESCE(venue_lat, %s),
                        venue_lon = COALESCE(venue_lon, %s),
                        updated_at = now()
                    WHERE golf_tournament_id = %s
                    """,
                    (city, country, coords[0], coords[1], int(tournament_id)),
                )
                summary["meteo_ecrite"] += 1
        connection.commit()
    finally:
        connection.close()
        client.close()
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
