"""Deploy the custom weather agent (variation 3) as an Azure Container App.

This is the "native hosting, outside Foundry" comparison point: the container
serves every protocol itself (responses / invocations / invocations_ws / a2a /
activity) and calls the weather MCP server **directly** (no toolbox, no auth).

Requires ``WEATHER_MCP_URL`` (run ``scripts.deploy_weather_mcp_server`` first).

    python -m scripts.deploy_custom_agent
"""

from __future__ import annotations

from scripts._helpers import (
    ROOT,
    acr_build,
    container_env_default_domain,
    deploy_container_app,
    env,
    load_env,
    save_env,
)

APP_NAME = "weather-custom-agent"
PORT = 8088


def main() -> None:
    load_env()
    image = acr_build(APP_NAME, ROOT / "src" / "custom_agent" / "Dockerfile")

    public_url = f"https://{APP_NAME}.{container_env_default_domain()}"
    container_env = {
        "WEATHER_TOOL_MODE": "direct",
        "WEATHER_MCP_URL": env("WEATHER_MCP_URL", required=True),
        "AZURE_AI_PROJECT_ENDPOINT": env("AZURE_AI_PROJECT_ENDPOINT", required=True),
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": env("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-4.1-mini"),
        "PUBLIC_BASE_URL": public_url,
    }
    conn = env("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if conn:
        container_env["APPLICATIONINSIGHTS_CONNECTION_STRING"] = conn

    url = deploy_container_app(
        APP_NAME,
        image,
        target_port=PORT,
        env_vars=container_env,
        readiness_path="/readiness",
    )
    save_env({"WEATHER_CUSTOM_AGENT_URL": url})
    print(f"\nCustom agent ready: {url}")
    print("Benchmark it with:")
    print(f"  python -m src.clients.run_benchmark --base-url {url}")


if __name__ == "__main__":
    main()
