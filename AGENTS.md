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
| hypothesis workflow agent         | Foundry-hosted container     | `scripts.deploy_hosted_hypothesis_agent`   | `HYPOTHESIS_HOSTED_AGENT_NAME` |

All scripts are run from the repo root as `python -m scripts.<name>`, read
`./.env`, and use `DefaultAzureCredential` / the Azure CLI login. Run
`az login` and `azd auth login` first.

## Environment variables

Written by `azd up` (copied to `./.env` by the `azure.yaml` postdeploy hook):

- `AZURE_AI_PROJECT_ENDPOINT` — Foundry project endpoint (all agents + toolbox)
- `AZURE_AI_ENDPOINT_TYPE=foundry|openai` — model inference route for all hosted/custom containers
- `AZURE_AI_MODEL_DEPLOYMENT_NAME` — chat model deployment
- `WEATHER_TOOL_MODE=direct|toolbox` — weather tool route for every agent; defaults to `direct`
- `APPLICATIONINSIGHTS_CONNECTION_STRING` — telemetry (all components)
- `AZURE_CONTAINER_REGISTRY_NAME` / `AZURE_CONTAINER_REGISTRY_ENDPOINT` — ACR
- `AZURE_CONTAINER_ENVIRONMENT_NAME` — Container Apps environment
- `AZURE_MANAGED_IDENTITY_NAME` — user-assigned identity (ACR pull for apps)
- `AZURE_RESOURCE_GROUP`, `AZURE_LOCATION`
- `AZURE_PRIVATE_NETWORKING` — whether the Foundry account is network injected
- `AZURE_VNET_NAME` / `AZURE_VNET_ID` / `AZURE_AGENT_SUBNET_ID` / `AZURE_SANDBOX_SUBNET_ID`

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
azd up            # first time (prompts for env name, region, subscription,
                  # and enablePrivateNetworking)
azd provision     # re-run infra only after editing infra/*.bicep
```

`enablePrivateNetworking` is prompted because `ENABLE_PRIVATE_NETWORKING` has
no default in `infra/main.parameters.json`. Answer it up front with
`azd env set ENABLE_PRIVATE_NETWORKING true|false`. When true, the AI Services
account gets `publicNetworkAccess=Disabled`, `networkInjections` into
`agent-subnet`, and private endpoints/DNS zones from
`infra/core/network/private-endpoints.bicep`; the endpoint is then only
reachable from inside the VNet (deploy the agents before enabling it, and
benchmark from the sandbox).

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

The hypothesis workflow is independent of the weather benchmark. Configure
`INTERNET_RESEARCH_MCP_URL`, `CONTEXT_API_MCP_URL`, and
`DOCUMENT_SEARCH_MCP_URL`, then run:

```bash
python -m scripts.register_hypothesis_toolboxes
python -m scripts.deploy_hosted_hypothesis_agent
```

Its invocations are two-phase: `plan` returns an `awaiting_approval` record;
`decide ... approved` must echo that record's exact revision and SHA-256 digest.
State is persisted in the provisioned Blob Storage account. Use
`python -m scripts.invoke_hypothesis_agent --help` for client commands.

Set `WEATHER_TOOL_MODE=toolbox` before the agent deploy commands to benchmark
the toolbox path. The default is `direct`. Redeploy every agent after changing
the mode; each deploy script records its effective mode for benchmark metadata.

The LangChain container uses the in-memory LangGraph Agent Server for this
benchmark. Its A2A endpoint is `/a2a/weather-agent`; a production standalone
Agent Server requires the backing services and license documented by LangChain.

## Common tasks

### Re-grant hosted-agent permissions

Shared hosted-agent services use the AI Services account's system-assigned
identity. Hosted versions can also expose a distinct instance identity; the
hypothesis-agent deploy grants that identity Foundry User, Cognitive Services
OpenAI User, and Storage Blob Data Contributor after activation. The command
below re-grants the shared account-level permissions without a redeploy:

```bash
python -m scripts.grant_agent_permissions               # toolbox role + Agent 365 OtelWrite
python -m scripts.grant_agent_permissions --skip-observability
python -m scripts.grant_agent_permissions --skip-toolbox
```

### Rebuild one image

```bash
python -m scripts.build_containers            # all containers, or:
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

### Run a benchmark batch in an ACA sandbox

```bash
python -m scripts.run_benchmark_sandbox --agents all --protocols all \
  --model-hosting foundry --iterations 20
python -m scripts.run_benchmark_sandbox --agents custom-maf --protocols responses \
  --model-hosting openai --iterations 10 --network public --results-dir results/batch-01
```

Boots a sandbox in the `sbx-$AZURE_ENV_NAME` sandbox group, downloads the repo
(`--repo-url`/`--repo-ref`, defaults to the `origin` remote at `HEAD`),
installs `src/clients/requirements.txt`, runs one benchmark per
agent/protocol combination and downloads all result files plus
`batch-summary.json`. `--network auto` (default) joins the sandbox to
`sandbox-subnet` when `AZURE_PRIVATE_NETWORKING=true`; `public` reaches the
agents through the ACA egress proxy under a deny-by-default allowlist derived
from `.env` (extend it with `--egress-allow`). The sandbox is deleted at the
end unless `--keep-sandbox` is passed. The commit under test must be pushed —
the sandbox downloads it from GitHub.

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

### Run the harness console

The harness is local and interactive; it is not built, deployed, or benchmarked
as a Container App.

```bash
pip install -r src/custom_agent_harness/requirements.txt
WEATHER_MCP_URL="$WEATHER_MCP_URL" python -m src.custom_agent_harness.agent
```

It starts in plan mode. Use `/mode execute` after approving the plan.

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
- **Sandbox exec/data-plane calls return 403** — the signed-in user needs
  `Container Apps SandboxGroup Data Owner` on the resource group; the script
  grants it and waits 60s, but propagation can take longer. Re-run it.
- **Sandbox benchmark gets 401 from Foundry** — the injected token expired
  (batches longer than an hour) or the run used `--network public` against a
  privately deployed account. Re-run, or switch to `--network private`.
- **No traces in App Insights** — confirm
  `APPLICATIONINSIGHTS_CONNECTION_STRING` is set for the component.
- **Protocol rejected on `deploy_hosted_agent_responses` / `deploy_hosted_agent_invocations`**
  — trim `PROTOCOLS` in the relevant script to what the target Foundry region
  supports (defaults to responses + a2a, and invocations + invocations_ws).

## Tear down

```bash
python -m scripts.delete_agents   # Foundry agents + toolbox
az resource delete --resource-group "$AZURE_RESOURCE_GROUP" \
  --resource-type Microsoft.App/sandboxGroups --name "sbx-$AZURE_ENV_NAME"
azd down                          # all Azure resources
```
