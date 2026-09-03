"""Run the latency benchmark from an Azure Container Apps sandbox.

The sandbox is a disposable Linux VM managed by the ACA sandbox service
(https://sandboxes.azure.com). This script drives one end-to-end batch:

1. create (or reuse) a sandbox group, optionally joined to the deployment's
   ``sandbox-subnet`` so the sandbox reaches privately deployed agents over the
   virtual network (``ENABLE_PRIVATE_NETWORKING=true`` at ``azd up`` time);
2. boot a sandbox and apply a deny-by-default egress policy that whitelists
   only the hosts the benchmark needs — when the agents are public, this is the
   ACA egress proxy route to them;
3. download this repository into the sandbox, install the benchmark client
   dependencies and write the resolved ``.env``;
4. run one ``src.clients.run_benchmark`` invocation per requested
   agent/protocol combination (batch mode);
5. download every result file the runs produced, plus a batch summary.

Examples
--------
Benchmark two agent variations over all their protocols, 20 iterations each::

    python -m scripts.run_benchmark_sandbox \
        --agents custom-maf,hosted-responses \
        --protocols all \
        --model-hosting foundry \
        --iterations 20

Every deployed agent, only the responses protocol, results in ./results/batch::

    python -m scripts.run_benchmark_sandbox --agents all --protocols responses \
        --model-hosting foundry --iterations 10 --results-dir results/batch
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from scripts._helpers import ROOT, env, load_env, run

# Data-plane role required to create sandboxes in a sandbox group.
SANDBOX_DATA_OWNER_ROLE = "Container Apps SandboxGroup Data Owner"
VNET_CONNECTION_NAME = "benchmark-vnet"
SANDBOX_WORKDIR = "/work"
SANDBOX_RESULTS_DIR = f"{SANDBOX_WORKDIR}/results"

# Hosts the sandbox always needs: the repository tarball and PyPI.
BOOTSTRAP_EGRESS_HOSTS = (
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "raw.githubusercontent.com",
    "pypi.org",
    "files.pythonhosted.org",
)
# Entra ID token endpoints (only used when the sandbox authenticates itself).
IDENTITY_EGRESS_HOSTS = ("login.microsoftonline.com", "login.microsoft.com")

# `.env` keys the benchmark clients read inside the sandbox.
FORWARDED_ENV_KEYS = (
    "AZURE_AI_PROJECT_ENDPOINT",
    "AZURE_AI_MODEL_DEPLOYMENT_NAME",
    "AZURE_OPENAI_CHAT_DEPLOYMENT_NAME",
    "OPENAI_API_VERSION",
    "WEATHER_TOOL_MODE",
    "WEATHER_PROMPT_AGENT_ID",
    "WEATHER_PROMPT_AGENT_TOOL_MODE",
    "WEATHER_HOSTED_AGENT_RESPONSES_NAME",
    "WEATHER_HOSTED_AGENT_RESPONSES_TOOL_MODE",
    "WEATHER_HOSTED_AGENT_INVOCATIONS_NAME",
    "WEATHER_HOSTED_AGENT_INVOCATIONS_TOOL_MODE",
    "WEATHER_CUSTOM_AGENT_MAF_URL",
    "WEATHER_CUSTOM_AGENT_MAF_TOOL_MODE",
    "WEATHER_CUSTOM_AGENT_LANGCHAIN_URL",
    "WEATHER_CUSTOM_AGENT_LANGCHAIN_TOOL_MODE",
)


# --------------------------------------------------------------------------- #
# Batch matrix
# --------------------------------------------------------------------------- #
@dataclass
class BatchRun:
    """One ``run_benchmark`` invocation inside the sandbox."""

    agent: str
    protocols: tuple[str, ...]
    result_file: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    downloaded: str | None = None


@dataclass
class SandboxSettings:
    """Everything the sandbox needs that is not derived from ``.env``."""

    resource_group: str
    sandbox_group: str
    location: str
    subnet_id: str = ""
    use_vnet: bool = False
    cpu: str = "2000m"
    memory: str = "4096Mi"
    extra_egress_hosts: tuple[str, ...] = field(default_factory=tuple)


def resolve_agents(requested: str, known: dict[str, dict]) -> list[str]:
    """Expand ``--agents`` into a validated list of agent variation names."""
    if requested.strip() == "all":
        return sorted(known)
    agents = [item.strip() for item in requested.split(",") if item.strip()]
    if not agents:
        raise SystemExit("--agents must name at least one agent variation.")
    unknown = [agent for agent in agents if agent not in known]
    if unknown:
        raise SystemExit(f"Unknown agent(s): {unknown}. Available: {sorted(known)}")
    return agents


def build_matrix(agents: list[str], protocols: str, known: dict[str, dict]) -> list[BatchRun]:
    """Pair every agent with the protocols it supports out of ``--protocols``."""
    requested = None
    if protocols.strip() != "all":
        requested = [item.strip() for item in protocols.split(",") if item.strip()]
        if not requested:
            raise SystemExit("--protocols must name at least one protocol or be 'all'.")

    matrix: list[BatchRun] = []
    for agent in agents:
        supported = tuple(known[agent]["protocols"])
        selected = supported if requested is None else tuple(p for p in requested if p in supported)
        if not selected:
            print(f"  ↳ skipping '{agent}': supports {list(supported)}, none requested")
            continue
        matrix.append(BatchRun(agent=agent, protocols=selected, result_file=f"benchmark-{agent}.json"))
    if not matrix:
        raise SystemExit("No agent/protocol combination left to benchmark.")
    return matrix


def benchmark_command(run_item: BatchRun, *, model_hosting: str, iterations: int, query: str, auth: str) -> str:
    """The shell command that runs one benchmark inside the sandbox."""
    args = [
        f"{SANDBOX_WORKDIR}/.venv/bin/python",
        "-m", "src.clients.run_benchmark",
        "--agent", run_item.agent,
        "--protocols", ",".join(run_item.protocols),
        "--model-hosting", model_hosting,
        "--iterations", str(iterations),
        "--query", query,
        "--auth", auth,
        "--out", f"{SANDBOX_RESULTS_DIR}/{run_item.result_file}",
    ]
    return " ".join(shlex.quote(arg) for arg in args)


def _host_of(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return parsed.hostname or ""


def agent_egress_hosts(values: dict[str, str]) -> list[str]:
    """Hosts of every deployed agent/tool endpoint found in the environment."""
    hosts = set()
    for key in (
        "AZURE_AI_PROJECT_ENDPOINT",
        "WEATHER_CUSTOM_AGENT_MAF_URL",
        "WEATHER_CUSTOM_AGENT_LANGCHAIN_URL",
        "WEATHER_MCP_URL",
        "FOUNDRY_TOOLBOX_ENDPOINT",
    ):
        host = _host_of(values.get(key, ""))
        if host:
            hosts.add(host)
    return sorted(hosts)


def egress_host_patterns(values: dict[str, str], extra: tuple[str, ...], *, include_identity: bool) -> list[str]:
    """Full deny-by-default allowlist for the sandbox."""
    patterns = list(BOOTSTRAP_EGRESS_HOSTS)
    if include_identity:
        patterns.extend(IDENTITY_EGRESS_HOSTS)
    patterns.extend(agent_egress_hosts(values))
    patterns.extend(host for host in extra if host)
    seen: list[str] = []
    for pattern in patterns:
        if pattern not in seen:
            seen.append(pattern)
    return seen


# --------------------------------------------------------------------------- #
# Azure plumbing
# --------------------------------------------------------------------------- #
def subscription_id() -> str:
    return env("AZURE_SUBSCRIPTION_ID") or run(
        ["az", "account", "show", "--query", "id", "-o", "tsv"], capture=True
    )


def ensure_sandbox_data_owner(subscription: str, resource_group: str) -> None:
    """Grant the signed-in user data-plane access to sandboxes in the group."""
    principal_id = run(["az", "ad", "signed-in-user", "show", "--query", "id", "-o", "tsv"], capture=True)
    if not principal_id:
        print("  ↳ no signed-in az CLI user; skipping sandbox role grant")
        return
    scope = f"/subscriptions/{subscription}/resourceGroups/{resource_group}"
    existing = run(
        ["az", "role", "assignment", "list", "--assignee", principal_id,
         "--scope", scope, "--role", SANDBOX_DATA_OWNER_ROLE, "--query", "[].id", "-o", "tsv"],
        capture=True,
    )
    if existing:
        print(f"  ↳ '{SANDBOX_DATA_OWNER_ROLE}' already assigned")
        return
    run(["az", "role", "assignment", "create", "--assignee-object-id", principal_id,
         "--assignee-principal-type", "User", "--role", SANDBOX_DATA_OWNER_ROLE, "--scope", scope])
    print(f"  ↳ assigned '{SANDBOX_DATA_OWNER_ROLE}'; waiting 60s for propagation")
    time.sleep(60)


def access_token(scope: str = "https://ai.azure.com/.default") -> str:
    """Entra ID token for the Foundry data plane, issued on the caller's machine."""
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential().get_token(scope).token


def build_egress_policy(host_patterns: list[str], token_rules: list[tuple[str, str]]):
    """Deny-by-default egress policy; ``token_rules`` inject bearer tokens at the proxy."""
    from azure.containerapps.sandbox import (
        EgressHeader,
        EgressHostRule,
        EgressPolicy,
        EgressRule,
        EgressRuleAction,
        EgressRuleMatch,
    )

    rules = [
        EgressRule(
            name=f"auth-{index}",
            match=EgressRuleMatch(host=host),
            action=EgressRuleAction(
                type="Transform",
                headers=[EgressHeader(operation="Set", name="Authorization", value=f"Bearer {token}")],
            ),
        )
        for index, (host, token) in enumerate(token_rules)
    ]
    # Hosts carrying an injected header must not also match a host rule: a host
    # rule short-circuits the transform pipeline.
    transformed = {host for host, _ in token_rules}
    return EgressPolicy(
        default_action="Deny",
        traffic_inspection="Full" if rules else "None",
        host_rules=[
            EgressHostRule(pattern=pattern, action="Allow")
            for pattern in host_patterns
            if pattern not in transformed
        ],
        rules=rules,
    )


def wait_for_exec(sandbox, *, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if sandbox.exec("true").exit_code == 0:
                return
        except Exception:  # noqa: BLE001 - the exec endpoint comes up asynchronously
            pass
        time.sleep(3)
    raise RuntimeError("sandbox exec endpoint did not come up in time")


def sandbox_exec(sandbox, command: str, *, label: str, check: bool = True):
    print(f"  ↳ [{label}] {command[:160]}")
    result = sandbox.exec(command, working_directory=SANDBOX_WORKDIR)
    if check and result.exit_code != 0:
        raise RuntimeError(
            f"sandbox exec failed [{label}]: exit={result.exit_code}\n"
            f"  out: {(result.stdout or '')[:800]}\n"
            f"  err: {(result.stderr or '')[:800]}"
        )
    return result


# --------------------------------------------------------------------------- #
# Sandbox bootstrap
# --------------------------------------------------------------------------- #
def repo_tarball_url(repo_url: str, ref: str) -> str:
    """``https://github.com/o/r(.git)`` + ref → codeload tarball URL."""
    path = urlparse(repo_url).path if "://" in repo_url else repo_url
    slug = path.strip("/").removesuffix(".git")
    if slug.count("/") != 1:
        raise SystemExit(f"--repo-url must point at a GitHub repository, got {repo_url!r}")
    return f"https://codeload.github.com/{slug}/tar.gz/{ref}"


def default_repo_url() -> str:
    return run(["git", "-C", str(ROOT), "remote", "get-url", "origin"], capture=True)


def default_repo_ref() -> str:
    return run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture=True)


def bootstrap_sandbox(sandbox, *, tarball_url: str, env_file: str) -> None:
    """Download the repo, install the benchmark client deps, write ``.env``."""
    sandbox_exec(sandbox, f"mkdir -p {SANDBOX_WORKDIR} {SANDBOX_RESULTS_DIR}", label="mkdir")
    sandbox_exec(
        sandbox,
        "curl -fsSL " + shlex.quote(tarball_url) + " -o /tmp/repo.tar.gz && "
        f"tar -xzf /tmp/repo.tar.gz --strip-components=1 -C {SANDBOX_WORKDIR}",
        label="download-repo",
    )
    sandbox_exec(
        sandbox,
        f"python3 -m venv {SANDBOX_WORKDIR}/.venv && "
        f"{SANDBOX_WORKDIR}/.venv/bin/pip install --quiet --upgrade pip && "
        f"{SANDBOX_WORKDIR}/.venv/bin/pip install --quiet -r {SANDBOX_WORKDIR}/src/clients/requirements.txt",
        label="install-deps",
    )
    sandbox.write_file(f"{SANDBOX_WORKDIR}/.env", env_file)
    print("  ↳ sandbox bootstrapped")


def sandbox_env_file(values: dict[str, str], token: str | None) -> str:
    """The ``.env`` written into the sandbox (never contains Azure credentials)."""
    lines = [f"{key}={values[key]}" for key in FORWARDED_ENV_KEYS if values.get(key)]
    if token:
        lines.append(f"AZURE_AI_ACCESS_TOKEN={token}")
    return "\n".join(lines) + "\n"


def download_results(sandbox, matrix: list[BatchRun], destination: Path) -> None:
    """Copy every result file produced in the sandbox to ``destination``."""
    destination.mkdir(parents=True, exist_ok=True)
    listing = sandbox.list_files(SANDBOX_RESULTS_DIR)
    names = {entry.name for entry in getattr(listing, "entries", []) or []}
    for item in matrix:
        if item.result_file not in names:
            print(f"  ↳ no result file for {item.agent} (run failed?)")
            continue
        content = sandbox.read_file(f"{SANDBOX_RESULTS_DIR}/{item.result_file}")
        target = destination / item.result_file
        target.write_bytes(content)
        item.downloaded = str(target)
        print(f"  ↳ downloaded {target}")


def write_summary(matrix: list[BatchRun], destination: Path, metadata: dict[str, object]) -> Path:
    summary = destination / "batch-summary.json"
    payload = {
        **metadata,
        "runs": [
            {
                "agent": item.agent,
                "protocols": list(item.protocols),
                "exit-code": item.exit_code,
                "result-file": item.downloaded,
                "stderr": item.stderr[-2000:],
            }
            for item in matrix
        ],
    }
    summary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return summary


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(known_agents: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--agents", required=True,
        help=f"Comma-separated agent variations to benchmark, or 'all'. Available: {known_agents}",
    )
    parser.add_argument(
        "--protocols", default="all",
        help="Comma-separated protocols, or 'all' (each agent runs the subset it supports).",
    )
    parser.add_argument("--model-hosting", choices=["foundry", "openai"], required=True,
                        help="Model inference endpoint used by the agents; recorded in every result.")
    parser.add_argument("--iterations", type=int, default=10, help="Warm/followup iterations per protocol.")
    parser.add_argument("--query", default="What is the current weather in Berlin?", help="Question asked each turn.")
    parser.add_argument("--results-dir", default=None,
                        help="Local directory for the downloaded results (default: results/sandbox-<UTC timestamp>).")
    parser.add_argument("--network", choices=["auto", "private", "public"], default="auto",
                        help="'private' joins the sandbox to the deployment VNet; 'public' goes through the ACA "
                             "egress proxy; 'auto' (default) follows AZURE_PRIVATE_NETWORKING.")
    parser.add_argument("--sandbox-group", default=None, help="Sandbox group name (default: sbx-$AZURE_ENV_NAME).")
    parser.add_argument("--sandbox-location", default=None, help="Sandbox region (default: $AZURE_LOCATION).")
    parser.add_argument("--resource-group", default=None, help="Resource group (default: $AZURE_RESOURCE_GROUP).")
    parser.add_argument("--cpu", default="2000m", help="Sandbox CPU request, e.g. 2000m.")
    parser.add_argument("--memory", default="4096Mi", help="Sandbox memory request, e.g. 4096Mi.")
    parser.add_argument("--repo-url", default=None, help="Repository to benchmark from (default: origin remote).")
    parser.add_argument("--repo-ref", default=None, help="Branch, tag or commit to download (default: HEAD).")
    parser.add_argument("--egress-allow", default="",
                        help="Extra comma-separated hosts to whitelist in the sandbox egress policy.")
    parser.add_argument("--keep-sandbox", action="store_true", help="Leave the sandbox running for inspection.")
    return parser.parse_args()


def main() -> int:
    values = load_env()
    from src.clients.run_benchmark import AGENTS

    args = parse_args(sorted(AGENTS))
    agents = resolve_agents(args.agents, AGENTS)
    matrix = build_matrix(agents, args.protocols, AGENTS)

    private_deployment = env("AZURE_PRIVATE_NETWORKING", "false").strip().lower() == "true"
    use_vnet = private_deployment if args.network == "auto" else args.network == "private"
    subnet_id = env("AZURE_SANDBOX_SUBNET_ID")
    if use_vnet and not subnet_id:
        raise SystemExit(
            "AZURE_SANDBOX_SUBNET_ID is not set — re-run `azd provision` to create the sandbox subnet, "
            "or pass --network public."
        )
    if private_deployment and not use_vnet:
        print("WARNING: the Foundry account is private; a public sandbox cannot reach it.")

    settings = SandboxSettings(
        resource_group=args.resource_group or env("AZURE_RESOURCE_GROUP", required=True),
        sandbox_group=args.sandbox_group or f"sbx-{env('AZURE_ENV_NAME') or 'foundry-performance'}",
        location=args.sandbox_location or env("AZURE_LOCATION", required=True),
        subnet_id=subnet_id,
        use_vnet=use_vnet,
        cpu=args.cpu,
        memory=args.memory,
        extra_egress_hosts=tuple(item.strip() for item in args.egress_allow.split(",") if item.strip()),
    )

    from azure.containerapps.sandbox import SandboxGroupClient, SandboxGroupManagementClient, endpoint_for_region
    from azure.identity import AzureCliCredential

    started_at = datetime.now(timezone.utc)
    results_dir = Path(args.results_dir or f"results/sandbox-{started_at.strftime('%Y%m%dT%H%M%SZ')}")
    subscription = subscription_id()
    credential = AzureCliCredential()
    labels = {"scenario": "foundry-performance", "run": uuid.uuid4().hex[:8]}

    print(f"Sandbox group : {settings.sandbox_group} ({settings.location}, rg={settings.resource_group})")
    print(f"Network       : {'vnet-injected' if use_vnet else 'public via egress proxy'}")
    print(f"Batch         : {[(item.agent, item.protocols) for item in matrix]}")

    management = SandboxGroupManagementClient(
        credential, subscription_id=subscription, resource_group=settings.resource_group
    )
    management.begin_create_group(settings.sandbox_group, location=settings.location, tags=labels).result()
    if use_vnet:
        management.create_or_update_vnet_connection(
            settings.sandbox_group, VNET_CONNECTION_NAME, settings.subnet_id, location=settings.location
        )
        print(f"  ↳ sandbox group joined to {settings.subnet_id.rsplit('/', 1)[-1]}")
    ensure_sandbox_data_owner(subscription, settings.resource_group)

    group_client = SandboxGroupClient(
        endpoint_for_region(settings.location),
        credential,
        subscription_id=subscription,
        resource_group=settings.resource_group,
        sandbox_group=settings.sandbox_group,
    )

    # Foundry endpoints require an Entra token. Over the VNet the traffic
    # bypasses the egress proxy, so the token travels in the sandbox `.env`
    # (read by src.clients.auth); over the public route the proxy injects the
    # Authorization header and the token never lands in the sandbox.
    foundry_host = _host_of(values.get("AZURE_AI_PROJECT_ENDPOINT", ""))
    needs_token = any(AGENTS[item.agent].get("default_auth") == "entra" for item in matrix)
    token = access_token() if needs_token else None
    token_rules = [(foundry_host, token)] if (needs_token and not use_vnet and foundry_host) else []
    policy = build_egress_policy(
        egress_host_patterns(values, settings.extra_egress_hosts, include_identity=use_vnet),
        token_rules,
    )

    sandbox = None
    exit_code = 0
    try:
        create = group_client.begin_create_sandbox(
            disk="ubuntu",
            cpu=settings.cpu,
            memory=settings.memory,
            labels=labels,
            egress_policy=policy,
            **({"customer_vnet_connection_name": VNET_CONNECTION_NAME} if use_vnet else {}),
        )
        sandbox = create.result()
        print(f"  ↳ sandbox {sandbox.sandbox_id} running")
        wait_for_exec(sandbox)

        bootstrap_sandbox(
            sandbox,
            tarball_url=repo_tarball_url(args.repo_url or default_repo_url(), args.repo_ref or default_repo_ref()),
            env_file=sandbox_env_file(values, token if (needs_token and use_vnet) else None),
        )

        for item in matrix:
            command = benchmark_command(
                item,
                model_hosting=args.model_hosting,
                iterations=args.iterations,
                query=args.query,
                # The proxy already injects Authorization when it transforms
                # the Foundry host; asking the client to add one as well would
                # need a credential the sandbox does not have.
                auth="none" if token_rules else "auto",
            )
            print(f"\n→ {item.agent} [{', '.join(item.protocols)}]")
            result = sandbox_exec(sandbox, command, label=item.agent, check=False)
            item.exit_code = result.exit_code
            item.stdout = result.stdout or ""
            item.stderr = result.stderr or ""
            print(item.stdout[-4000:])
            if item.exit_code != 0:
                exit_code = 1
                print(f"  ↳ FAILED (exit={item.exit_code})\n{item.stderr[-2000:]}")

        print("\n### Downloading results")
        download_results(sandbox, matrix, results_dir)
        summary = write_summary(
            matrix,
            results_dir,
            {
                "datetime": started_at.isoformat().replace("+00:00", "Z"),
                "model-hosting": args.model_hosting,
                "iterations": args.iterations,
                "query": args.query,
                "network": "vnet" if use_vnet else "public",
                "sandbox-group": settings.sandbox_group,
            },
        )
        print(f"  ↳ wrote {summary}")
    finally:
        if sandbox is not None and not args.keep_sandbox:
            try:
                sandbox.delete()
                print(f"\n🗑️  deleted sandbox {sandbox.sandbox_id}")
            except Exception as error:  # noqa: BLE001 - cleanup must not mask the run result
                print(f"WARNING: could not delete sandbox: {error}")
        elif sandbox is not None:
            print(f"\nSandbox kept: {sandbox.sandbox_id} (delete it when done)")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
