"""Random-but-plausible weather data generator for the weather MCP server.

The **city catalog** is static data shipped with the container (``cities.json``).
The **readings** (current conditions and multi-day forecast) are generated
randomly around each city's base temperature so the benchmark scenario always
has something to return without any external weather API dependency.

A process-wide seed (``WEATHER_SEED``) makes a single container instance return
stable values, which keeps latency measurements about the *transport* rather
than noisy payloads. Set ``WEATHER_SEED`` to a fixed integer for reproducible
runs, or leave it unset for fresh random data on every startup.
"""

from __future__ import annotations

import json
import os
import random
from datetime import date, timedelta
from pathlib import Path

_CATALOG_PATH = Path(__file__).with_name("cities.json")

_CONDITIONS = [
    "clear sky",
    "partly cloudy",
    "overcast",
    "light rain",
    "heavy rain",
    "thunderstorm",
    "snow",
    "fog",
    "windy",
]


def _rng() -> random.Random:
    seed = os.getenv("WEATHER_SEED")
    return random.Random(int(seed)) if seed and seed.strip().lstrip("-").isdigit() else random.Random()


def _load_catalog() -> list[dict]:
    with _CATALOG_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def _reading(rng: random.Random, base_temp_c: float) -> dict:
    """One weather reading randomised around a city's base temperature."""
    temp = round(base_temp_c + rng.uniform(-6.0, 6.0), 1)
    return {
        "temperature_c": temp,
        "feels_like_c": round(temp + rng.uniform(-3.0, 2.0), 1),
        "condition": rng.choice(_CONDITIONS),
        "humidity_percent": rng.randint(30, 95),
        "wind_kph": round(rng.uniform(0, 45), 1),
        "precipitation_mm": round(max(0.0, rng.uniform(-4, 12)), 1),
    }


class WeatherStore:
    """In-memory weather dataset built once per process from the city catalog."""

    def __init__(self) -> None:
        rng = _rng()
        self._catalog = _load_catalog()
        self._by_city = {c["city"].lower(): c for c in self._catalog}
        # Pre-generate the current reading + a 7-day forecast per city.
        self._current: dict[str, dict] = {}
        self._forecast: dict[str, list[dict]] = {}
        today = date.today()
        for c in self._catalog:
            key = c["city"].lower()
            base = c["base_temp_c"]
            self._current[key] = _reading(rng, base)
            self._forecast[key] = [
                {
                    "date": (today + timedelta(days=d)).isoformat(),
                    **_reading(rng, base),
                }
                for d in range(7)
            ]

    def cities(self) -> list[dict]:
        return [{"city": c["city"], "country": c["country"], "climate": c["climate"]} for c in self._catalog]

    def _resolve(self, city: str) -> dict | None:
        return self._by_city.get(city.strip().lower())

    def current(self, city: str) -> dict | None:
        meta = self._resolve(city)
        if not meta:
            return None
        return {
            "city": meta["city"],
            "country": meta["country"],
            "climate": meta["climate"],
            **self._current[meta["city"].lower()],
        }

    def forecast(self, city: str, days: int) -> dict | None:
        meta = self._resolve(city)
        if not meta:
            return None
        days = max(1, min(days, 7))
        return {
            "city": meta["city"],
            "country": meta["country"],
            "days": days,
            "forecast": self._forecast[meta["city"].lower()][:days],
        }
