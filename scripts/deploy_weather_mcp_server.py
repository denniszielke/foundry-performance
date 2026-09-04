"""Deploy the weather MCP server as an Azure Container App (no auth).

Builds the image, deploys it via the shared ``app.bicep`` module, and records
the resulting ``/mcp`` URL in ``.env`` (``WEATHER_MCP_URL``) for the custom agents
and the toolbox registration.

The MCP server is pinned to a single always-on replica (``minReplicas=1``) so it
is not itself a cold-start variable when benchmarking the agents.

    python -m scripts.deploy_weather_mcp_server
"""

from __future__ import annotations

from scripts._helpers import ROOT, acr_build, deploy_container_app, env, load_env, save_env, tag_from_cli

APP_NAME = "weather-mcp-server"
PORT = 8093


def main(tag: str | None = None) -> None:
    load_env()
    image = acr_build(APP_NAME, ROOT / "src" / "weather_mcp_server" / "Dockerfile", tag=tag)

    container_env = {
        "WEATHER_MCP_HOST": "0.0.0.0",
        "WEATHER_MCP_PORT": str(PORT),
        # Logged at startup so you can tell which build is actually running.
        "IMAGE_TAG": image.rsplit(":", 1)[-1],
    }
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
        max_replicas=1,
    )
    mcp_url = url.rstrip("/") + "/mcp"
    save_env({"WEATHER_MCP_URL": mcp_url, "WEATHER_MCP_IMAGE": image})
    print(f"\nWeather MCP server ready: {mcp_url}")


if __name__ == "__main__":
    main(tag_from_cli())
