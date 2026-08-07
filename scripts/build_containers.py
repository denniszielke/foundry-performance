"""Build all container images for the scenario in ACR.

Uses ``az acr build`` so no local Docker daemon is required. Every image is
tagged with a unique ``<git-sha>-<utc-ts>`` tag (or ``IMAGE_TAG`` / ``--tag`` when
given) rather than ``latest``, and the resulting refs are written to ``.env``
(``WEATHER_MCP_IMAGE`` / ``WEATHER_HOSTED_AGENT_RESPONSES_IMAGE`` /
``WEATHER_HOSTED_AGENT_INVOCATIONS_IMAGE`` / ``WEATHER_CUSTOM_AGENT_MAF_IMAGE`` /
``WEATHER_CUSTOM_AGENT_LANGCHAIN_IMAGE``)::

    python -m scripts.build_containers
    python -m scripts.build_containers --tag v2
    IMAGE_TAG=v2 python -m scripts.build_containers
"""

from __future__ import annotations

from scripts._helpers import ROOT, acr_build, load_env, resolve_image_tag, save_env, tag_from_cli

IMAGES = [
    ("weather-mcp-server", ROOT / "src" / "weather_mcp_server" / "Dockerfile", "WEATHER_MCP_IMAGE"),
    ("weather-hosted-agent-responses", ROOT / "src" / "hosted_agent_responses" / "Dockerfile", "WEATHER_HOSTED_AGENT_RESPONSES_IMAGE"),
    ("weather-hosted-agent-invocations", ROOT / "src" / "hosted_agent_invocations" / "Dockerfile", "WEATHER_HOSTED_AGENT_INVOCATIONS_IMAGE"),
    ("scenario-hosted-hypothesis-agent", ROOT / "src" / "hosted_hypothesis_agent" / "Dockerfile", "HYPOTHESIS_HOSTED_AGENT_IMAGE"),
    ("weather-custom-agent-maf", ROOT / "src" / "custom_agent_maf" / "Dockerfile", "WEATHER_CUSTOM_AGENT_MAF_IMAGE"),
    ("weather-custom-agent-langchain", ROOT / "src" / "custom_agent_langchain" / "Dockerfile", "WEATHER_CUSTOM_AGENT_LANGCHAIN_IMAGE"),
]


def main(tag: str | None = None) -> None:
    load_env()
    # Resolve once so all images share the same tag for this build.
    tag = resolve_image_tag(tag)
    refs: dict[str, str] = {}
    for image, dockerfile, env_key in IMAGES:
        refs[env_key] = acr_build(image, dockerfile, tag=tag)
    save_env(refs)
    print(f"\nBuilt all container images with tag '{tag}'.")


if __name__ == "__main__":
    main(tag_from_cli())
