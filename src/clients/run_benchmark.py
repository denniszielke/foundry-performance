"""Benchmark runner for the weather agents.

Drives one or more protocols against a running agent endpoint and reports
latency, time-to-first-token (TTFB) and cold-vs-warm-vs-followup behaviour so
the agent variations can be compared apples-to-apples.

``.env`` (repo root) is loaded automatically, so ``--agent`` alone is enough to
target any deployed variation — no need to construct a ``--base-url`` by hand
or re-export env vars in the shell first.

Examples
--------
Hosted agent, responses variation (base URL + auth derived from ``.env``)::

    python -m src.clients.run_benchmark --agent hosted-responses \
        --protocols responses --model-hosting foundry

Custom MAF agent (Container App, anonymous), all protocols::

    python -m src.clients.run_benchmark --agent custom-maf \
        --protocols all --model-hosting openai

Point at an arbitrary URL instead (e.g. a local process)::

    python -m src.clients.run_benchmark --base-url http://127.0.0.1:8088 \
        --protocols responses,invocations_ws --model-hosting foundry \
        --iterations 20 --out results.json

Phases
------
* ``cold``     — the first request against the agent (pays process/tool startup).
* ``warm``     — steady-state requests, each on a brand-new conversation.
* ``followup`` — a second turn that reuses a primed conversation.

Hosted session modes
--------------------
* ``dedicated`` — create one Foundry platform session per logical workload.
* ``shared``    — route every workload in the run through one platform session.

Foundry still provisions an isolated sandbox per platform session in both modes;
these labels describe benchmark routing, not agent deployment modes.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import json
import os
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

from src.clients.common import Stat, Timer, Turn, format_table, summarize
from src.clients.protocols import AgUiInvocationsClient, CLIENTS, LangGraphA2aClient, ResponsesClient

DEFAULT_QUERY = "What is the current weather in Berlin?"
FOLLOWUP_QUERY = "And what about the forecast for the next three days there?"
RESULTS_DIR = Path("results")

# One entry per `scripts.deploy_*` variation. `id_env`/`url_env` name the .env
# var that script writes; base URLs and auth are derived from these, not
# passed on the command line.
AGENTS: dict[str, dict] = {
    "prompt": {
        "id_env": "WEATHER_PROMPT_AGENT_ID",
        "tool_mode_env": "WEATHER_PROMPT_AGENT_TOOL_MODE",
        "protocols": ("responses", "responses-store", "a2a", "invocations"),
        "default_auth": "entra",
    },
    "hosted-responses": {
        "id_env": "WEATHER_HOSTED_AGENT_RESPONSES_NAME",
        "tool_mode_env": "WEATHER_HOSTED_AGENT_RESPONSES_TOOL_MODE",
        "protocols": ("responses", "responses-store", "a2a"),
        "default_auth": "entra",
    },
    "hosted-invocations": {
        "id_env": "WEATHER_HOSTED_AGENT_INVOCATIONS_NAME",
        "tool_mode_env": "WEATHER_HOSTED_AGENT_INVOCATIONS_TOOL_MODE",
        "protocols": ("invocations",),
        "default_auth": "entra",
    },
    "custom-langchain": {
        "url_env": "WEATHER_CUSTOM_AGENT_LANGCHAIN_URL",
        "tool_mode_env": "WEATHER_CUSTOM_AGENT_LANGCHAIN_TOOL_MODE",
        "protocols": ("responses", "a2a"),
        "default_auth": "none",
    },
    "custom-maf": {
        "url_env": "WEATHER_CUSTOM_AGENT_MAF_URL",
        "tool_mode_env": "WEATHER_CUSTOM_AGENT_MAF_TOOL_MODE",
        "protocols": tuple(protocol for protocol in CLIENTS if protocol != "responses-store"),
        "default_auth": "none",
    },
}

# The hosted invocations agent deliberately implements AG-UI, whereas Foundry
# prompt agents and the custom MAF agent use the platform's native JSON invocation
# contract. Keep the benchmark label as `invocations` while selecting the
# matching transport client for each implementation.
AGENT_PROTOCOL_CLIENTS = {
    "hosted-invocations": {"invocations": AgUiInvocationsClient},
    "custom-langchain": {"a2a": LangGraphA2aClient},
}


def _require_env(name: str, hint: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"${name} is not set ({hint}).")
    return value


def _resolve_base_url(agent: str, protocol: str) -> str:
    """Build the base URL for one (agent, protocol) pair from env vars."""
    cfg = AGENTS[agent]
    if "url_env" in cfg:
        return _require_env(cfg["url_env"], f"run the deploy script for '{agent}'")

    endpoint = _require_env("AZURE_AI_PROJECT_ENDPOINT", "run `azd up`")
    agent_id = _require_env(cfg["id_env"], f"run the matching `python -m scripts.deploy_*` for '{agent}'")
    # The OpenAI-compatible responses protocol lives one path segment deeper
    # than the other native protocols (a2a, invocations, invocations_ws).
    suffix = "/openai" if protocol.startswith("responses") else ""
    return f"{endpoint}/agents/{agent_id}/endpoint/protocols{suffix}"


async def _timed(
    client,
    query: str,
    conversation_id: str | None,
    agent_session_id: str | None,
) -> tuple[Timer, str]:
    timer = Timer()
    text = await client.call(timer, query, conversation_id, agent_session_id)
    return timer, text


async def _bench_protocol(
    name: str,
    base_url: str,
    model: str,
    iterations: int,
    query: str,
    auth: httpx.Auth | None,
    session_mode: str = "not-applicable",
    client_cls=None,
) -> list[Turn]:
    cls = client_cls or CLIENTS[name]
    turns: list[Turn] = []
    make = (
        (lambda: cls(base_url, model, auth=auth))
        if issubclass(cls, ResponsesClient)
        else (lambda: cls(base_url, auth=auth))
    )

    async with make() as client:
        shared_session = await client.create_session() if session_mode == "shared" else None

        async def workload_session() -> str | None:
            if session_mode == "shared":
                return shared_session
            if session_mode == "dedicated":
                return await client.create_session()
            return None

        # cold — first request against the agent.
        turns.append(await _run_one(client, name, "cold", query, str(uuid.uuid4()), await workload_session()))

        # warm — fresh conversation each iteration; sandbox routing follows session_mode.
        for _ in range(iterations):
            turns.append(await _run_one(client, name, "warm", query, str(uuid.uuid4()), await workload_session()))

        # followup — prime a conversation, then measure its second turn.
        for _ in range(iterations):
            conversation_id = str(uuid.uuid4())
            agent_session_id = await workload_session()
            try:
                await _timed(client, query, conversation_id, agent_session_id)  # prime (discarded)
            except Exception:  # noqa: BLE001 - if priming fails the followup will record the error
                pass
            turns.append(
                await _run_one(client, name, "followup", FOLLOWUP_QUERY, conversation_id, agent_session_id)
            )

    return turns


async def _run_one(
    client,
    protocol: str,
    phase: str,
    query: str,
    conversation_id: str | None,
    agent_session_id: str | None,
) -> Turn:
    try:
        timer, text = await _timed(client, query, conversation_id, agent_session_id)
        return Turn(
            protocol=protocol,
            phase=phase,
            ok=True,
            total_s=timer.elapsed_s,
            ttfb_s=timer.ttfb_s,
            text=text,
        )
    except Exception as exc:  # noqa: BLE001 - record failures, keep benchmarking
        return Turn(protocol=protocol, phase=phase, ok=False, total_s=0.0, error=f"{type(exc).__name__}: {exc}")


async def run(
    protocols: list[str],
    model: str,
    iterations: int,
    query: str,
    auth: httpx.Auth | None,
    *,
    agent: str | None = None,
    base_url: str | None = None,
    session_mode: str = "not-applicable",
) -> list[Turn]:
    turns: list[Turn] = []
    for name in protocols:
        url = base_url if base_url is not None else _resolve_base_url(agent, name)  # type: ignore[arg-type]
        print(f"→ benchmarking {name} ({url}) ...", flush=True)
        client_cls = AGENT_PROTOCOL_CLIENTS.get(agent or "", {}).get(name)
        turns.extend(await _bench_protocol(name, url, model, iterations, query, auth, session_mode, client_cls))
    return turns


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark the weather agent across protocols.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--agent",
        choices=sorted(AGENTS),
        help="Agent variation to benchmark; base URL(s) and default auth are derived from .env.",
    )
    target.add_argument(
        "--base-url",
        help="Explicit base URL (bypasses --agent/.env derivation), e.g. http://127.0.0.1:8088",
    )
    parser.add_argument(
        "--protocols",
        default="all",
        help=f"Comma-separated subset of {sorted(CLIENTS)} or 'all' (with --agent, 'all' means all it supports).",
    )
    parser.add_argument("--iterations", type=int, default=10, help="Warm/followup iterations per protocol.")
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "Model name for the responses protocol's request body. Defaults to "
            "$AZURE_AI_MODEL_DEPLOYMENT_NAME (falls back to 'weather-agent' if unset). Foundry's "
            "native agent-scoped responses endpoint (prompt agent, hosted-responses) rejects a "
            "mismatched model with a 400."
        ),
    )
    parser.add_argument(
        "--model-hosting",
        choices=["foundry", "openai"],
        required=True,
        help="Model inference endpoint used by the agent; recorded in the result artifact.",
    )
    parser.add_argument(
        "--tool-mode",
        choices=["direct", "toolbox"],
        default=None,
        help="Weather tool route recorded in the result. Defaults to the deployed agent's saved mode, then direct.",
    )
    parser.add_argument(
        "--session-mode",
        choices=["dedicated", "shared"],
        default=None,
        help=(
            "Hosted-agent sandbox routing: dedicated creates one platform session per workload; "
            "shared reuses one platform session for the run. Only valid with --agent hosted-* ."
        ),
    )
    parser.add_argument("--query", default=DEFAULT_QUERY, help="Question to ask each turn.")
    parser.add_argument(
        "--out",
        default=None,
        help="Result JSON path. Defaults to results/benchmark-<UTC timestamp>.json.",
    )
    parser.add_argument(
        "--auth",
        choices=["auto", "none", "entra"],
        default="auto",
        help=(
            "Attach an Entra ID bearer token (scope https://ai.azure.com/.default) via "
            "DefaultAzureCredential. 'auto' (default) picks the right mode for --agent "
            "(entra for prompt/hosted-*, none for custom); required when using --base-url "
            "directly against a Foundry endpoint."
        ),
    )
    return parser.parse_args()


def _milliseconds(seconds: float | None) -> float | None:
    if seconds is None or not math.isfinite(seconds):
        return None
    return round(seconds * 1000, 3)


def _result_payload(
    *,
    recorded_at: datetime,
    agent_type: str,
    model_hosting: str,
    model_deployment: str,
    tool_mode: str,
    session_mode: str,
    iterations: int,
    query: str,
    base_url: str | None,
    turns: list[Turn],
    stats: list[Stat],
) -> dict[str, object]:
    recorded_at_text = recorded_at.isoformat().replace("+00:00", "Z")
    results = [
        {
            "agent-type": agent_type,
            "protocol": stat.protocol,
            "phase": stat.phase,
            "model-hosting": model_hosting,
            "model-deployment": model_deployment,
            "tool-mode": tool_mode,
            "session-mode": session_mode,
            "n": stat.count,
            "err": stat.errors,
            "mean-ms": _milliseconds(stat.mean_s),
            "p50": _milliseconds(stat.p50_s),
            "p95": _milliseconds(stat.p95_s),
            "ttfb": _milliseconds(stat.mean_ttfb_s),
        }
        for stat in stats
    ]
    return {
        "datetime": recorded_at_text,
        "agent-type": agent_type,
        "model-hosting": model_hosting,
        "model-deployment": model_deployment,
        "tool-mode": tool_mode,
        "session-mode": session_mode,
        "iterations": iterations,
        "query": query,
        "base-url": base_url,
        "results": results,
        "turns": [asdict(turn) for turn in turns],
    }


def _result_path(out: str | None, recorded_at: datetime) -> Path:
    if out:
        return Path(out)
    timestamp = recorded_at.strftime("%Y%m%dT%H%M%S.%fZ")
    return RESULTS_DIR / f"benchmark-{timestamp}.json"


def main() -> None:
    from scripts._helpers import load_env

    load_env()  # merge repo-root .env into os.environ (does not override already-set vars)

    args = _parse_args()
    model = args.model or os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "weather-agent")

    cfg = AGENTS[args.agent] if args.agent else None
    is_hosted_agent = bool(args.agent and args.agent.startswith("hosted-"))
    if args.session_mode and not is_hosted_agent:
        raise SystemExit("--session-mode is only supported with --agent hosted-*.")
    session_mode = args.session_mode or ("dedicated" if is_hosted_agent else "not-applicable")
    tool_mode = args.tool_mode or (
        os.environ.get(cfg["tool_mode_env"], os.environ.get("WEATHER_TOOL_MODE", "direct"))
        if cfg
        else os.environ.get("WEATHER_TOOL_MODE", "direct")
    )
    supported = cfg["protocols"] if cfg else tuple(CLIENTS)
    if args.protocols == "all":
        protocols = list(supported)
    else:
        protocols = [p.strip() for p in args.protocols.split(",") if p.strip()]
        unknown = [p for p in protocols if p not in CLIENTS]
        if unknown:
            raise SystemExit(f"Unknown protocol(s): {unknown}. Available: {sorted(CLIENTS)}")
        if cfg:
            unsupported = [p for p in protocols if p not in supported]
            if unsupported:
                raise SystemExit(f"'{args.agent}' doesn't support {unsupported}. Supported: {sorted(supported)}")

    auth_mode = args.auth
    if auth_mode == "auto":
        auth_mode = cfg["default_auth"] if cfg else "none"

    auth = None
    if auth_mode == "entra":
        from src.clients.auth import EntraTokenAuth

        auth = EntraTokenAuth()

    recorded_at = datetime.now(timezone.utc)
    turns = asyncio.run(
        run(
            protocols,
            model,
            args.iterations,
            args.query,
            auth,
            agent=args.agent,
            base_url=args.base_url,
            session_mode=session_mode,
        )
    )
    stats = summarize(turns)

    print()
    print(format_table(stats))

    errors = [t for t in turns if not t.ok]
    if errors:
        seen: dict[str, str] = {}
        for t in errors:
            seen.setdefault(t.error or "unknown error", f"{t.protocol}/{t.phase}")
        print("\nErrors seen:")
        for error, where in seen.items():
            print(f"  [{where}] {error}")

    agent_type = args.agent or "base-url"
    payload = _result_payload(
        recorded_at=recorded_at,
        agent_type=agent_type,
        model_hosting=args.model_hosting,
        model_deployment=model,
        tool_mode=tool_mode,
        session_mode=session_mode,
        iterations=args.iterations,
        query=args.query,
        base_url=args.base_url,
        turns=turns,
        stats=stats,
    )
    result_path = _result_path(args.out, recorded_at)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    with result_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, allow_nan=False)
        fh.write("\n")
    print(f"\nWrote {len(turns)} turns to {result_path}")


if __name__ == "__main__":
    main()
