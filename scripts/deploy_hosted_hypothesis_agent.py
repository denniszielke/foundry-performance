"""Build and deploy the hosted hypothesis workflow on invocations protocol 2.0.0."""

from __future__ import annotations

import time

from scripts._helpers import (
    ROOT,
    acr_build,
    ensure_openai_role,
    ensure_storage_blob_role,
    ensure_toolbox_role,
    ensure_toolbox_role_for_hosted_agent,
    env,
    load_env,
    project_client,
    save_env,
    storage_account_name,
    tag_from_cli,
)

AGENT_NAME = "scenario-hosted-hypothesis-agent"
PROTOCOLS = [("invocations", "2.0.0")]


def main(tag: str | None = None) -> None:
    load_env()
    image = acr_build(AGENT_NAME, ROOT / "src" / "hosted_hypothesis_agent" / "Dockerfile", tag=tag)
    ensure_toolbox_role()
    ensure_storage_blob_role()

    storage_name = storage_account_name()
    container_env = {
        "AZURE_AI_PROJECT_ENDPOINT": env("AZURE_AI_PROJECT_ENDPOINT", required=True),
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": env("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-4.1-mini"),
        "WORKFLOW_STORAGE_ACCOUNT_URL": f"https://{storage_name}.blob.core.windows.net",
        "WORKFLOW_STORAGE_CONTAINER": env("WORKFLOW_STORAGE_CONTAINER", "agent-workflows"),
        "WORKFLOW_TTL_HOURS": env("WORKFLOW_TTL_HOURS", "24"),
        "HARNESS_MAX_ITERATIONS": env("HARNESS_MAX_ITERATIONS", "12"),
        "INTERNET_RESEARCH_TOOLBOX_ENDPOINT": env("INTERNET_RESEARCH_TOOLBOX_ENDPOINT", required=True),
        "CONTEXT_API_TOOLBOX_ENDPOINT": env("CONTEXT_API_TOOLBOX_ENDPOINT", required=True),
        "DOCUMENT_SEARCH_TOOLBOX_ENDPOINT": env("DOCUMENT_SEARCH_TOOLBOX_ENDPOINT", required=True),
        "INTERNET_RESEARCH_ALLOWED_TOOLS": env("INTERNET_RESEARCH_ALLOWED_TOOLS"),
        "CONTEXT_API_ALLOWED_TOOLS": env("CONTEXT_API_ALLOWED_TOOLS"),
        "DOCUMENT_SEARCH_ALLOWED_TOOLS": env("DOCUMENT_SEARCH_ALLOWED_TOOLS"),
        "IMAGE_TAG": image.rsplit(":", 1)[-1],
    }

    from azure.ai.projects.models import (
        AgentEndpointConfig,
        ContainerConfiguration,
        FixedRatioVersionSelectionRule,
        HostedAgentDefinition,
        InvocationsProtocolConfiguration,
        ProtocolConfiguration,
        ProtocolVersionRecord,
        VersionSelector,
    )

    with project_client() as client:
        created = client.agents.create_version(
            agent_name=AGENT_NAME,
            definition=HostedAgentDefinition(
                cpu=env("HOSTED_AGENT_CPU", "1"),
                memory=env("HOSTED_AGENT_MEMORY", "2Gi"),
                container_configuration=ContainerConfiguration(image=image),
                environment_variables=container_env,
                protocol_versions=[ProtocolVersionRecord(protocol=protocol, version=version) for protocol, version in PROTOCOLS],
            ),
            description="Human-approved hypothesis planning and execution workflow.",
            metadata={"enableVnextExperience": "true"},
            headers={"Foundry-Features": "HostedAgents=V1Preview"},
        )
        print(f"Created hosted agent '{AGENT_NAME}' version {created.version}; waiting for Active")
        deadline = time.time() + 900
        while time.time() < deadline:
            current = client.agents.get_version(agent_name=AGENT_NAME, agent_version=created.version)
            status = getattr(current, "status", None)
            status_value = getattr(status, "value", status)
            print(f"  status={status}")
            if status_value == "active":
                break
            if status_value in ("failed", "deleting", "deleted"):
                raise SystemExit(f"Hosted agent version entered terminal status: {status}")
            time.sleep(15)
        else:
            raise SystemExit("Timed out waiting for the hosted agent to become Active.")

        instance_identity = getattr(current, "instance_identity", None)
        principal_id = getattr(instance_identity, "principal_id", "")
        ensure_toolbox_role_for_hosted_agent(principal_id)
        ensure_openai_role(principal_id)
        ensure_storage_blob_role(principal_id)

        client.agents.update_details(
            agent_name=AGENT_NAME,
            agent_endpoint=AgentEndpointConfig(
                version_selector=VersionSelector(
                    version_selection_rules=[
                        FixedRatioVersionSelectionRule(agent_version=created.version, traffic_percentage=100)
                    ]
                ),
                protocol_configuration=ProtocolConfiguration(
                    invocations=InvocationsProtocolConfiguration(),
                ),
            ),
        )

    save_env({
        "HYPOTHESIS_HOSTED_AGENT_NAME": AGENT_NAME,
        "HYPOTHESIS_HOSTED_AGENT_IMAGE": image,
    })
    print(f"Hosted hypothesis agent ready: {AGENT_NAME} ({image})")


if __name__ == "__main__":
    main(tag_from_cli())