"""Build all container images for the scenario in ACR.

Uses ``az acr build`` so no local Docker daemon is required. Run after `azd up`::

    python -m scripts.build_containers
"""

from __future__ import annotations

from scripts._helpers import ROOT, acr_build, load_env

IMAGES = [
    ("weather-mcp-server", ROOT / "src" / "weather_mcp_server" / "Dockerfile"),
    ("weather-hosted-agent", ROOT / "src" / "hosted_agent" / "Dockerfile"),
    ("weather-custom-agent", ROOT / "src" / "custom_agent" / "Dockerfile"),
]


def main() -> None:
    load_env()
    for image, dockerfile in IMAGES:
        acr_build(image, dockerfile)
    print("\nBuilt all container images.")


if __name__ == "__main__":
    main()
