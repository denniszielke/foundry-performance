# AGENTS.md — operations runbook

Operational tasks for the Foundry performance benchmark. For the overview and
architecture see [README.md](README.md).

## Components and where they run

| component                          | runs on                     | deploy script                             | key env written |
|------------------------------------|-----------------------------|--------------------------------------------|-----------------|
| weather MCP server                | Azure Container App          | `scripts.deploy_weather_mcp_server`        | `WEATHER_MCP_URL` |
| weather toolbox                   | Foundry (management plane)   | `scripts.register_weather_toolbox`         | `WEATHER_TOOLBOX_NAME`, `FOUNDRY_TOOLBOX_ENDPOINT` |
| prompt agent (var 1)              | Foundry-native              | `scripts.deploy_prompt_agent`              | `WEATHER_PROMPT_AGENT_ID` |
| hosted agent, responses (var 2)   | Foundry-hosted container    | `scripts.deploy_hosted_agent_responses`    | `WEATHER_HOSTED_AGENT_RESPONSES_NAME` |
| hosted agent, invocations (var 3) | Foundry-hosted container    | `scripts.deploy_hosted_agent_invocations`  | `WEATHER_HOSTED_AGENT_INVOCATIONS_NAME` |
| custom MAF agent (var 4)          | Azure Container App          | `scripts.deploy_custom_agent_maf`          | `WEATHER_CUSTOM_AGENT_MAF_URL` |
| custom LangChain agent (var 5)    | Azure Container App          | `scripts.deploy_custom_agent_langchain`    | `WEATHER_CUSTOM_AGENT_LANGCHAIN_URL` |

All scripts are run from the repo root as `python -m scripts.<name>`, read
`./.env`, and use `DefaultAzureCredential` / the Azure CLI login. Run
`az login` and `azd auth login` first.

## Environment variables

Written by `azd up` (copied to `./.env` by the `azure.yaml` postdeploy hook):

- `AZURE_AI_PROJECT_ENDPOINT` — Foundry project endpoint (all agents + toolbox)
- `AZURE_AI_ENDPOINT_TYPE=foundry|openai` — model inference route for all hosted/custom containers
- `AZURE_AI_MODEL_DEPLOYMENT_NAME` — chat model deployment
- `APPLICATIONINSIGHTS_CONNECTION_STRING` — telemetry (all components)
- `AZURE_CONTAINER_REGISTRY_NAME` / `AZURE_CONTAINER_REGISTRY_ENDPOINT` — ACR
- `AZURE_CONTAINER_ENVIRONMENT_NAME` — Container Apps environment
- `AZURE_MANAGED_IDENTITY_NAME` — user-assigned identity (ACR pull for apps)
- `AZURE_RESOURCE_GROUP`, `AZURE_LOCATION`

Optional deployment sizing (defaults shown):

- `HOSTED_AGENT_CPU=1`, `HOSTED_AGENT_MEMORY=2Gi` — Foundry-hosted containers
- `CUSTOM_AGENT_CPU=1`, `CUSTOM_AGENT_MEMORY=2.0Gi` — custom Container App

Written by the deploy scripts: `WEATHER_MCP_URL`, `WEATHER_TOOLBOX_NAME`,
`FOUNDRY_TOOLBOX_ENDPOINT`, `WEATHER_PROMPT_AGENT_ID`,
`WEATHER_HOSTED_AGENT_RESPONSES_NAME`, `WEATHER_HOSTED_AGENT_INVOCATIONS_NAME`,
`WEATHER_CUSTOM_AGENT_MAF_URL`, `WEATHER_CUSTOM_AGENT_LANGCHAIN_URL`.

See `.env.sample` for the complete list.

## Provision infrastructure

```bash
azd up            # first time (prompts for env name, region, subscription)
azd provision     # re-run infra only after editing infra/*.bicep
```

Region must be `northcentralus` while `invocations_ws` is in preview.
`ENABLE_HOSTED_AGENTS=true` (default) creates the ACR + Foundry ACR connection
required for the hosted agents.

## Full deploy (order matters)

```bash
pip install -r requirements.txt
python -m scripts.build_containers
python -m scripts.deploy_weather_mcp_server        # must precede toolbox + agents
python -m scripts.register_weather_toolbox         # must precede the hosted agents
python -m scripts.deploy_prompt_agent
python -m scripts.deploy_hosted_agent_responses
python -m scripts.deploy_hosted_agent_invocations
python -m scripts.deploy_custom_agent_maf
python -m scripts.deploy_custom_agent_langchain
```

The LangChain container uses the in-memory LangGraph Agent Server for this
benchmark. Its A2A endpoint is `/a2a/weather-agent`; a production standalone
Agent Server requires the backing services and license documented by LangChain.

## Common tasks

### Re-grant hosted-agent permissions

The hosted agents run as the AI Services account's system-assigned identity
(there's no per-agent Entra Agent Identity in this repo). Redundant with the
inline grant in the deploy scripts, but useful to re-check or re-apply without
a redeploy:

```bash
python -m scripts.grant_agent_permissions               # toolbox role + Agent 365 OtelWrite
python -m scripts.grant_agent_permissions --skip-observability
python -m scripts.grant_agent_permissions --skip-toolbox
```

### Rebuild one image

```bash
python -m scripts.build_containers            # all four, or:
az acr build --registry "$AZURE_CONTAINER_REGISTRY_NAME" \
  --image weather-custom-agent-maf:latest \
  --file src/custom_agent_maf/Dockerfile .
```

### Redeploy a Container App (MCP server / custom agents)

Re-run the deploy script — the underlying `app.bicep` deployment is idempotent
and rolls out a new revision:

```bash
python -m scripts.deploy_weather_mcp_server
python -m scripts.deploy_custom_agent_maf
python -m scripts.deploy_custom_agent_langchain
```

### Update a Foundry agent (new version)

Re-running `deploy_prompt_agent` / `deploy_hosted_agent_responses` /
`deploy_hosted_agent_invocations` calls `agents.create_version(...)` again and
routes 100% of traffic to the new version. Each hosted agent script polls until
the new version is `Active` before switching traffic.

### Update the toolbox

Re-run `register_weather_toolbox`; it deletes the existing toolbox and creates a
fresh version pointing at the current `WEATHER_MCP_URL`. Redeploy the hosted
agents afterward so they pick up the updated toolbox.

### Run the benchmark

```bash
python -m src.clients.run_benchmark --agent custom-maf --protocols all --model-hosting foundry
python -m src.clients.run_benchmark --base-url http://127.0.0.1:8088 \
  --protocols responses,invocations_ws --model-hosting foundry \
  --iterations 20 --out results.json
```

## Run locally (no Azure hosting for the container)

```bash
# terminal 1 — weather MCP server
WEATHER_MCP_HOST=127.0.0.1 python -m src.weather_mcp_server.server

# terminal 2 — custom MAF agent against the local MCP server
WEATHER_MCP_URL=http://127.0.0.1:8093/mcp \
AZURE_AI_PROJECT_ENDPOINT="$AZURE_AI_PROJECT_ENDPOINT" \
AZURE_AI_MODEL_DEPLOYMENT_NAME="$AZURE_AI_MODEL_DEPLOYMENT_NAME" \
python -m src.custom_agent_maf.agent

# terminal 3 — benchmark
python -m src.clients.run_benchmark --base-url http://127.0.0.1:8088 \
  --model-hosting foundry
```

The agent still needs a real Foundry project + model for inference
(`AZURE_AI_PROJECT_ENDPOINT`) and an authenticated `az login`.

## Troubleshoot

- **Agent can't reach the tool** — check `WEATHER_MCP_URL` resolves and
  `GET $WEATHER_MCP_URL/../health` returns `{"status":"ok"}`. The MCP server runs
  with `minReplicas=1` so it should not cold-start.
- **Hosted agent stuck not `Active`** — inspect the version status in the Foundry
  portal; confirm Foundry can pull the image from ACR (the ACR connection is
  created when `ENABLE_HOSTED_AGENTS=true`).
- **`invocations_ws` fails** — confirm the region is `northcentralus`.
- **No traces in App Insights** — confirm
  `APPLICATIONINSIGHTS_CONNECTION_STRING` is set for the component.
- **Protocol rejected on `deploy_hosted_agent_responses` / `deploy_hosted_agent_invocations`**
  — trim `PROTOCOLS` in the relevant script to what the target Foundry region
  supports (defaults to responses + a2a, and invocations + invocations_ws).

## Tear down

```bash
python -m scripts.delete_agents   # Foundry agents + toolbox
azd down                          # all Azure resources
```
