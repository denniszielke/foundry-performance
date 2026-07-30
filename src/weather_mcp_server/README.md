# Weather MCP server

A [Model Context Protocol](https://modelcontextprotocol.io) server that exposes
**weather tools** to every agent variation in this benchmark. It is the single
tool surface the agents call to answer weather questions.

The server has **no external data dependency** — the city catalog is static
(`cities.json`) and the readings are randomly generated around each city's base
temperature (see `weather_data.py`). It also has **no authentication**: the
benchmark measures transport/hosting latency, so the connection is kept as fast
as possible (anonymous, no Entra JWT, no Easy Auth).

## Tools

| Tool | Purpose |
| --- | --- |
| `list_cities` | List the cities the server can report weather for |
| `get_current_weather` | Current temperature, condition, humidity, wind, precipitation for a city |
| `get_forecast` | 1–7 day forecast for a city |
| `propose_activity` | Suggest activities for a city and expected weather conditions |
| `propose_city` | Rank cities by desired weather/date, environment, or both |

`propose_city` combines the two proposal use cases in one MCP-safe tool because
tool names must be unique. Pass `conditions` with an optional ISO `date`, pass
`environment`, or pass both. Dates are limited to the generated seven-day
forecast. All temperatures are in degrees Celsius. Results are JSON.

## Run locally

```bash
python -m src.weather_mcp_server.server
```

Serves streamable-HTTP MCP at `http://127.0.0.1:8093/mcp` and a readiness probe
at `http://127.0.0.1:8093/health`. Override the bind address with
`WEATHER_MCP_HOST` / `WEATHER_MCP_PORT`. Set `WEATHER_SEED` to a fixed integer
for reproducible random readings.

## Deploy to Azure Container Apps

```bash
# Build the image in ACR, then deploy (public ingress, no auth)
python -m scripts.deploy_weather_mcp_server --build

# Deploy only — image already in ACR
python -m scripts.deploy_weather_mcp_server

# Build, deploy, then register the Foundry toolbox in one go
python -m scripts.deploy_weather_mcp_server --build --register
```

| Variable | Description | Default |
| --- | --- | --- |
| `WEATHER_MCP_APP_NAME` | Container App name | `weather-mcp-server` |
| `WEATHER_MCP_PORT` | Container port | `8093` |
| `WEATHER_MCP_EXTERNAL` | Expose the app externally (`true`/`false`) | `true` |
| `WEATHER_SEED` | Optional int seed for reproducible readings | unset |
