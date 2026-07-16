"""Connecteur Open-Meteo — meteo previsionnelle des matchs a venir.

Gratuit, sans cle. Deux endpoints :
  - geocoding-api.open-meteo.com : ville -> coordonnees
  - api.open-meteo.com/v1/forecast : prevision horaire (jusqu'a 16 jours)

Le facteur WEATHER produit est une valeur [-1, 0] du contrat context.py :
negatif = conditions qui depriment le jeu offensif (pluie forte, vent,
chaleur extreme, gel). Meteo clemente = 0 (aucun ajustement).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def weather_factor(
    temperature_c: float | None,
    precipitation_mm: float | None,
    wind_speed_kmh: float | None,
) -> tuple[float, str]:
    """(valeur [-1, 0], note lisible) a partir des conditions au coup d'envoi.

    Seuils conservateurs, alignes sur la litterature foot :
      - pluie : penalite des 1 mm/h, saturee a 6 mm/h (deluge)
      - vent : penalite au-dela de 25 km/h, saturee a 55 km/h
      - temperature : penalite au-dela de 30 C ou en-dessous de -3 C
    """
    penalty = 0.0
    notes: list[str] = []

    if precipitation_mm is not None and precipitation_mm > 0.5:
        severity = min(1.0, precipitation_mm / 6.0)
        penalty += 0.50 * severity
        notes.append(f"pluie {precipitation_mm:.1f} mm/h")

    if wind_speed_kmh is not None and wind_speed_kmh > 25.0:
        severity = min(1.0, (wind_speed_kmh - 25.0) / 30.0)
        penalty += 0.35 * severity
        notes.append(f"vent {wind_speed_kmh:.0f} km/h")

    if temperature_c is not None:
        if temperature_c > 30.0:
            severity = min(1.0, (temperature_c - 30.0) / 10.0)
            penalty += 0.30 * severity
            notes.append(f"chaleur {temperature_c:.0f} C")
        elif temperature_c < -3.0:
            severity = min(1.0, (-3.0 - temperature_c) / 10.0)
            penalty += 0.30 * severity
            notes.append(f"froid {temperature_c:.0f} C")

    value = -min(1.0, penalty)
    note = ", ".join(notes) if notes else "conditions normales"
    return round(value, 3), note


class OpenMeteoClient:
    def __init__(self, timeout_seconds: float = 15.0) -> None:
        self._client = httpx.Client(timeout=timeout_seconds)
        self._geocode_cache: dict[str, tuple[float, float] | None] = {}

    def close(self) -> None:
        self._client.close()

    def geocode(self, city: str, country: str | None = None) -> tuple[float, float] | None:
        key = f"{city}|{country or ''}".lower()
        if key in self._geocode_cache:
            return self._geocode_cache[key]
        try:
            response = self._client.get(GEOCODE_URL, params={"name": city, "count": 5})
            response.raise_for_status()
            results = response.json().get("results") or []
        except (httpx.HTTPError, ValueError):
            results = []
        coords: tuple[float, float] | None = None
        if results:
            # Preferer le resultat du bon pays quand on le connait.
            chosen = results[0]
            if country:
                for r in results:
                    if str(r.get("country", "")).lower() == country.lower():
                        chosen = r
                        break
            coords = (float(chosen["latitude"]), float(chosen["longitude"]))
        self._geocode_cache[key] = coords
        return coords

    def forecast_at(
        self, lat: float, lon: float, kickoff_utc: datetime,
    ) -> dict[str, Any] | None:
        """Conditions prevues a l'heure du coup d'envoi (UTC)."""
        kickoff = kickoff_utc if kickoff_utc.tzinfo else kickoff_utc.replace(tzinfo=timezone.utc)
        day = kickoff.strftime("%Y-%m-%d")
        try:
            response = self._client.get(FORECAST_URL, params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "temperature_2m,precipitation,wind_speed_10m",
                "timezone": "UTC",
                "start_date": day,
                "end_date": day,
            })
            response.raise_for_status()
            hourly = response.json().get("hourly") or {}
        except (httpx.HTTPError, ValueError):
            return None
        times = hourly.get("time") or []
        target = kickoff.strftime("%Y-%m-%dT%H:00")
        if target not in times:
            return None
        i = times.index(target)

        def _at(series_name: str) -> float | None:
            series = hourly.get(series_name) or []
            return float(series[i]) if i < len(series) and series[i] is not None else None

        return {
            "temperature_c": _at("temperature_2m"),
            "precipitation_mm": _at("precipitation"),
            "wind_speed_kmh": _at("wind_speed_10m"),
        }
