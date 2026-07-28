"""Weather MCP server — the single tool surface for the benchmark scenario.

A [Model Context Protocol](https://modelcontextprotocol.io) server that exposes
**weather tools** backed by randomly generated data (see ``weather_data.py``).
It is the tool every agent variation calls to answer weather questions.

Design notes:
- **No authentication.** The benchmark measures transport/hosting latency, so
  the server runs fully anonymous (no Entra JWT, no Easy Auth) to keep the
  connection as fast as possible.
- Streamable-HTTP transport at ``/mcp``; readiness probe at ``/health``.

Run locally from the project root::

    python -m src.weather_mcp_server.server

Environment variables:
  WEATHER_MCP_HOST   bind address (default 127.0.0.1; 0.0.0.0 in container)
  WEATHER_MCP_PORT   bind port (default 8093)
  WEATHER_SEED       optional int seed for reproducible random readings
  APPLICATIONINSIGHTS_CONNECTION_STRING   optional OpenTelemetry tracing
"""

from __future__ import annotations

import json
import logging
import os

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.weather_mcp_server.weather_data import WeatherStore

_HOST = os.getenv("WEATHER_MCP_HOST", "127.0.0.1")
_PORT = int(os.getenv("WEATHER_MCP_PORT", "8093"))

logger = logging.getLogger("weather_mcp_server")

# Optional Application Insights instrumentation (README: all components trace).
if os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(logger_name="weather_mcp_server")
    except Exception:  # noqa: BLE001 - tracing is best-effort, never fatal
        logger.warning("Application Insights instrumentation could not be configured", exc_info=True)

_store = WeatherStore()

mcp = FastMCP(
    name="weather",
    instructions=(
        "Weather information tools. Use these tools to look up the current weather "
        "and multi-day forecast for a city. Call list_cities first if you are unsure "
        "which cities are available. All temperatures are in degrees Celsius. Results "
        "are returned as JSON."
    ),
)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(_: Request) -> JSONResponse:
    """Readiness probe endpoint — returns 200 OK when the server is up."""
    return JSONResponse({"status": "ok"})


@mcp.tool()
def list_cities() -> str:
    """List the cities this server can report weather for.

    Returns a JSON array of ``{city, country, climate}`` objects.
    """
    return json.dumps({"cities": _store.cities()}, indent=2)


@mcp.tool()
def get_current_weather(city: str) -> str:
    """Get the current weather for a city.

    Args:
        city: City name, e.g. "Berlin" or "Tokyo" (case-insensitive).

    Returns JSON with temperature (°C), feels-like, condition, humidity, wind and
    precipitation. Returns an error object if the city is not known — call
    ``list_cities`` to discover valid names.
    """
    reading = _store.current(city)
    if reading is None:
        return json.dumps(
            {"error": f"Unknown city '{city}'.", "known_cities": [c["city"] for c in _store.cities()]},
            indent=2,
        )
    return json.dumps(reading, indent=2)


@mcp.tool()
def get_forecast(city: str, days: int = 3) -> str:
    """Get a multi-day weather forecast for a city.

    Args:
        city: City name (case-insensitive).
        days: Number of days to forecast, 1-7 (default 3).

    Returns JSON with a per-day forecast. Returns an error object if the city is
    not known — call ``list_cities`` to discover valid names.
    """
    forecast = _store.forecast(city, days)
    if forecast is None:
        return json.dumps(
            {"error": f"Unknown city '{city}'.", "known_cities": [c["city"] for c in _store.cities()]},
            indent=2,
        )
    return json.dumps(forecast, indent=2)


def main() -> None:
    """Entry point — serve the weather tools over streamable-HTTP MCP."""
    logging.basicConfig(level=os.environ.get("MCP_LOG_LEVEL", "INFO"))
    logger.info("Starting weather MCP server — image_tag=%s", os.getenv("IMAGE_TAG", "unknown"))
    # `host_origin_protection` (DNS-rebinding guard) would reject the container's
    # non-localhost Host header; disable it when the running FastMCP supports the
    # kwarg, and fall back gracefully on versions that don't accept it.
    try:
        mcp.run(transport="http", host=_HOST, port=_PORT, host_origin_protection=False)
    except TypeError:
        mcp.run(transport="http", host=_HOST, port=_PORT)


if __name__ == "__main__":
    main()
