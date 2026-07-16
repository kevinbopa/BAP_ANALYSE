"""Meteo previsionnelle des matchs a venir (Open-Meteo, gratuit).

    py services/ingestion/run_ingest_weather.py

Pour chaque fixture des 10 prochains jours dont la ville du stade est connue
(capturee par le crawler contexte), geocode la ville puis lit la prevision a
l'heure du coup d'envoi -> ecrit le facteur WEATHER (source API) dans
core.fixture_context_factors. Le moteur le consomme automatiquement via
context.py (xG des deux equipes, plafonne 0.12).
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str((Path(__file__).resolve().parent / "src").resolve()))

from spe_ingestion.config import TheSportsDBSettings  # declenche le .env
from spe_ingestion.db import DatabaseSettings, connect_db
from spe_ingestion.openmeteo import OpenMeteoClient, weather_factor

TheSportsDBSettings.from_env()


def main() -> int:
    client = OpenMeteoClient()
    connection = connect_db(DatabaseSettings.from_env(prefix="POSTGRES_INGEST"))
    processed = written = no_geo = no_forecast = 0
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT f.fixture_id, f.kickoff_utc, f.venue_city, f.venue_country
                FROM core.fixtures f
                WHERE f.kickoff_utc >= now()
                  AND f.kickoff_utc <= now() + interval '10 days'
                  AND f.venue_city IS NOT NULL
                ORDER BY f.kickoff_utc
                """
            )
            fixtures = cursor.fetchall()

            for fixture_id, kickoff, city, country in fixtures:
                processed += 1
                coords = client.geocode(str(city), str(country) if country else None)
                if coords is None:
                    no_geo += 1
                    continue
                conditions = client.forecast_at(coords[0], coords[1], kickoff)
                if conditions is None:
                    no_forecast += 1
                    continue
                value, note = weather_factor(
                    conditions["temperature_c"],
                    conditions["precipitation_mm"],
                    conditions["wind_speed_kmh"],
                )
                cursor.execute(
                    """
                    INSERT INTO core.fixture_context_factors (
                        fixture_id, factor_code, factor_value, weight, source_code, note
                    )
                    VALUES (%s, 'WEATHER', %s, 1.0, 'API', %s)
                    ON CONFLICT (fixture_id, factor_code, source_code) DO UPDATE
                    SET factor_value = EXCLUDED.factor_value,
                        note = EXCLUDED.note,
                        created_at = now()
                    """,
                    (int(fixture_id), value, note),
                )
                written += 1
        connection.commit()
    finally:
        connection.close()
        client.close()

    print(json.dumps({
        "fixtures_examines": processed,
        "meteo_ecrite": written,
        "ville_introuvable": no_geo,
        "prevision_indisponible": no_forecast,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
