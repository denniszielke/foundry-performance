"""Deploy the weather MCP server as an Azure Container App (no auth).

Builds the image, deploys it via the shared ``app.bicep`` module, and records
the resulting ``/mcp`` URL in ``.env`` (``WEATHER_MCP_URL``) for the custom agent
and the toolbox registration.

The MCP server is pinned to a single always-on replica (``minReplicas=1``) so it
is not itself a cold-start variable when benchmarking the agents.

    python -m scripts.deploy_weather_mcp_server
"""

from __future__ import annotations

from scripts._helpers import ROOT, acr_build, deploy_container_app, env, load_env, save_env

APP_NAME = "weather-mcp-server"
PORT = 8093


def main() -> None:
    load_env()
    image = acr_build(APP_NAME, ROOT / "src" / "weather_mcp_server" / "Dockerfile")

    container_env = {"WEATHER_MCP_HOST": "0.0.0.0", "WEATHER_MCP_PORT": str(PORT)}
    conn = env("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if conn:
        container_env["APPLICATIONINSIGHTS_CONNECTION_STRING"] = conn

    url = deploy_container_app(
        APP_NAME,
        image,
        target_port=PORT,
        env_vars=container_env,
        readiness_path="/health",
        min_replicas=1,
    )
    mcp_url = url.rstrip("/") + "/mcp"
    save_env({"WEATHER_MCP_URL": mcp_url})
    print(f"\nWeather MCP server ready: {mcp_url}")


if __name__ == "__main__":
    main()
