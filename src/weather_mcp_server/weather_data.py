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
from typing import Any

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

_ENVIRONMENT_ALIASES = {
    "ocean": "sea",
    "seaside": "sea",
    "mountain": "mountains",
    "alpine": "mountains",
    "woods": "forest",
    "woodland": "forest",
    "urban": "city",
}

_ACTIVITIES = {
    "clear": ["walking tour", "outdoor sightseeing", "picnic"],
    "rain": ["museum visit", "covered market", "local food tour"],
    "snow": ["winter sightseeing", "cafe visit", "photography walk"],
    "wind": ["sheltered neighborhood tour", "museum visit", "cafe visit"],
    "fog": ["museum visit", "local food tour", "indoor cultural experience"],
    "default": ["walking tour", "local food tour", "museum visit"],
}


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
        return [
            {
                "city": c["city"],
                "country": c["country"],
                "climate": c["climate"],
                "environments": c["environments"],
            }
            for c in self._catalog
        ]

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

    def propose_activity(self, city: str, conditions: str) -> dict:
        """Suggest weather-appropriate activities for a known city."""
        meta = self._resolve(city)
        if not meta:
            return {"error": f"Unknown city '{city}'.", "known_cities": [c["city"] for c in self._catalog]}

        normalized = conditions.strip().lower()
        category = "default"
        for candidate, keywords in (
            ("rain", ("rain", "wet", "storm", "shower")),
            ("snow", ("snow", "freezing", "icy")),
            ("wind", ("wind", "breezy", "gust")),
            ("fog", ("fog", "mist")),
            ("clear", ("clear", "sun", "dry", "warm", "hot")),
        ):
            if any(keyword in normalized for keyword in keywords):
                category = candidate
                break

        activities = list(_ACTIVITIES[category])
        environments = set(meta["environments"])
        if category == "clear" and environments & {"sea", "coast", "beach", "island"}:
            activities.insert(0, "waterfront or beach visit")
        if category == "clear" and environments & {"mountains", "highlands", "forest"}:
            activities.insert(0, "hiking")

        return {
            "city": meta["city"],
            "country": meta["country"],
            "conditions": conditions,
            "environments": meta["environments"],
            "activities": activities[:3],
        }

    def propose_city(
        self,
        *,
        conditions: str | None = None,
        on_date: str | None = None,
        environment: str | None = None,
    ) -> dict:
        """Rank cities by desired forecast conditions and/or environment."""
        if not conditions and not environment:
            return {"error": "Provide conditions, environment, or both."}

        target_date = date.today()
        if on_date:
            try:
                target_date = date.fromisoformat(on_date)
            except ValueError:
                return {"error": "date must use ISO format YYYY-MM-DD."}
        day_index = (target_date - date.today()).days
        if day_index < 0 or day_index > 6:
            return {
                "error": "date must be within the available 7-day forecast.",
                "available_from": date.today().isoformat(),
                "available_to": (date.today() + timedelta(days=6)).isoformat(),
            }

        desired_environment = None
        if environment:
            desired_environment = _ENVIRONMENT_ALIASES.get(environment.strip().lower(), environment.strip().lower())

        ranked: list[dict[str, Any]] = []
        for meta in self._catalog:
            reading = self._forecast[meta["city"].lower()][day_index]
            weather_score, weather_matches = _score_conditions(reading, conditions)
            environment_match = desired_environment is None or desired_environment in meta["environments"]
            if desired_environment and not environment_match:
                continue
            ranked.append(
                {
                    "city": meta["city"],
                    "country": meta["country"],
                    "date": reading["date"],
                    "weather": reading,
                    "environments": meta["environments"],
                    "matched_conditions": weather_matches,
                    "score": weather_score + (1 if desired_environment else 0),
                }
            )

        ranked.sort(key=lambda item: (-item["score"], item["city"]))
        return {
            "requested_conditions": conditions,
            "requested_environment": environment,
            "date": target_date.isoformat(),
            "proposals": ranked[:3],
        }


def _score_conditions(reading: dict, conditions: str | None) -> tuple[int, list[str]]:
    if not conditions:
        return 0, []

    desired = conditions.lower()
    matches: list[str] = []
    condition = reading["condition"]
    temperature = reading["temperature_c"]
    precipitation = reading["precipitation_mm"]
    checks = [
        ("clear", ("clear" in condition or "partly cloudy" in condition), ("clear", "sunny", "sun")),
        ("cloudy", ("cloud" in condition or "overcast" in condition), ("cloud", "cloudy", "overcast")),
        ("rain", ("rain" in condition or "thunderstorm" in condition), ("rain", "rainy", "wet")),
        ("snow", "snow" in condition, ("snow", "snowy")),
        ("windy", ("windy" in condition or reading["wind_kph"] >= 25), ("windy", "wind")),
        ("fog", "fog" in condition, ("fog", "foggy")),
        ("hot", temperature >= 25, ("hot",)),
        ("warm", temperature >= 18, ("warm",)),
        ("mild", 10 <= temperature <= 24, ("mild",)),
        ("cool", temperature <= 15, ("cool",)),
        ("cold", temperature <= 8, ("cold",)),
        ("dry", precipitation <= 1 and not any(word in condition for word in ("rain", "snow", "storm")), ("dry",)),
        ("humid", reading["humidity_percent"] >= 70, ("humid",)),
        ("calm", reading["wind_kph"] <= 15, ("calm",)),
    ]
    requested = 0
    for label, is_match, keywords in checks:
        if any(keyword in desired for keyword in keywords):
            requested += 1
            if is_match:
                matches.append(label)
    if requested == 0 and condition in desired:
        matches.append(condition)
    return len(matches), matches
