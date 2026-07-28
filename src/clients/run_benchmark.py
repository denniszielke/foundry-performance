"""Benchmark runner for the weather agents.

Drives one or more protocols against a running agent endpoint and reports
latency, time-to-first-token (TTFB) and cold-vs-warm-vs-followup behaviour so
the four agent variations can be compared apples-to-apples.

``.env`` (repo root) is loaded automatically, so ``--agent`` alone is enough to
target any deployed variation — no need to construct a ``--base-url`` by hand
or re-export env vars in the shell first.

Examples
--------
Hosted agent, responses variation (base URL + auth derived from ``.env``)::

    python -m src.clients.run_benchmark --agent hosted-responses --protocols responses

Custom agent (Container App, anonymous), all protocols::

    python -m src.clients.run_benchmark --agent custom --protocols all

Point at an arbitrary URL instead (e.g. a local process)::

    python -m src.clients.run_benchmark --base-url http://127.0.0.1:8088 \
        --protocols responses,invocations_ws --iterations 20 --out results.json

Phases
------
* ``cold``     — the first request against the agent (pays process/tool startup).
* ``warm``     — steady-state requests, each on a brand-new session.
* ``followup`` — a second turn that reuses a primed session (conversation state).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from dataclasses import asdict

import httpx

from src.clients.common import Stat, Timer, Turn, format_table, summarize
from src.clients.protocols import AgUiInvocationsClient, CLIENTS, ResponsesClient

DEFAULT_QUERY = "What is the current weather in Berlin?"
FOLLOWUP_QUERY = "And what about the forecast for the next three days there?"

# One entry per `scripts.deploy_*` variation. `id_env`/`url_env` name the .env
# var that script writes; base URLs and auth are derived from these, not
# passed on the command line.
AGENTS: dict[str, dict] = {
    "prompt": {
        "id_env": "WEATHER_PROMPT_AGENT_ID",
        "protocols": ("responses", "a2a", "invocations"),
        "default_auth": "entra",
    },
    "hosted-responses": {
        "id_env": "WEATHER_HOSTED_AGENT_RESPONSES_NAME",
        "protocols": ("responses", "a2a"),
        "default_auth": "entra",
    },
    "hosted-invocations": {
        "id_env": "WEATHER_HOSTED_AGENT_INVOCATIONS_NAME",
        "protocols": ("invocations", "invocations_ws"),
        "default_auth": "entra",
    },
    "custom": {
        "url_env": "WEATHER_CUSTOM_AGENT_URL",
        "protocols": tuple(CLIENTS),
        "default_auth": "none",
    },
}

# The hosted invocations agent deliberately implements AG-UI, whereas Foundry
# prompt agents and the custom agent use the platform's native JSON invocation
# contract. Keep the benchmark label as `invocations` while selecting the
# matching transport client for each implementation.
AGENT_PROTOCOL_CLIENTS = {
    "hosted-invocations": {"invocations": AgUiInvocationsClient},
}


def _require_env(name: str, hint: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"${name} is not set ({hint}).")
    return value


def _resolve_base_url(agent: str, protocol: str) -> str:
    """Build the base URL for one (agent, protocol) pair from env vars."""
    cfg = AGENTS[agent]
    if agent == "custom":
        return _require_env(cfg["url_env"], "run `python -m scripts.deploy_custom_agent`")

    endpoint = _require_env("AZURE_AI_PROJECT_ENDPOINT", "run `azd up`")
    agent_id = _require_env(cfg["id_env"], f"run the matching `python -m scripts.deploy_*` for '{agent}'")
    # The OpenAI-compatible responses protocol lives one path segment deeper
    # than the other native protocols (a2a, invocations, invocations_ws).
    suffix = "/openai" if protocol == "responses" else ""
    return f"{endpoint}/agents/{agent_id}/endpoint/protocols{suffix}"


async def _timed(client, query: str, session_id: str | None) -> tuple[Timer, str]:
    timer = Timer()
    text = await client.call(timer, query, session_id)
    return timer, text


async def _bench_protocol(
    name: str,
    base_url: str,
    model: str,
    iterations: int,
    query: str,
    auth: httpx.Auth | None,
    client_cls=None,
) -> list[Turn]:
    cls = client_cls or CLIENTS[name]
    turns: list[Turn] = []
    make = (
        (lambda: cls(base_url, model, auth=auth)) if cls is ResponsesClient else (lambda: cls(base_url, auth=auth))
    )

    async with make() as client:
        # cold — first request against the agent.
        turns.append(await _run_one(client, name, "cold", query, str(uuid.uuid4())))

        # warm — fresh session each iteration.
        for _ in range(iterations):
            turns.append(await _run_one(client, name, "warm", query, str(uuid.uuid4())))

        # followup — prime a session, then measure the second turn.
        for _ in range(iterations):
            session = str(uuid.uuid4())
            try:
                await _timed(client, query, session)  # prime (discarded)
            except Exception:  # noqa: BLE001 - if priming fails the followup will record the error
                pass
            turns.append(await _run_one(client, name, "followup", FOLLOWUP_QUERY, session))

    return turns


async def _run_one(client, protocol: str, phase: str, query: str, session_id: str | None) -> Turn:
    try:
        timer, text = await _timed(client, query, session_id)
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
) -> list[Turn]:
    turns: list[Turn] = []
    for name in protocols:
        url = base_url if base_url is not None else _resolve_base_url(agent, name)  # type: ignore[arg-type]
        print(f"→ benchmarking {name} ({url}) ...", flush=True)
        client_cls = AGENT_PROTOCOL_CLIENTS.get(agent or "", {}).get(name)
        turns.extend(await _bench_protocol(name, url, model, iterations, query, auth, client_cls))
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
    parser.add_argument("--query", default=DEFAULT_QUERY, help="Question to ask each turn.")
    parser.add_argument("--out", default=None, help="Optional path to write raw turns + stats as JSON.")
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


def main() -> None:
    from scripts._helpers import load_env

    load_env()  # merge repo-root .env into os.environ (does not override already-set vars)

    args = _parse_args()
    model = args.model or os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "weather-agent")

    cfg = AGENTS[args.agent] if args.agent else None
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

    turns = asyncio.run(
        run(protocols, model, args.iterations, args.query, auth, agent=args.agent, base_url=args.base_url)
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

    if args.out:
        payload = {
            "agent": args.agent,
            "base_url": args.base_url,
            "iterations": args.iterations,
            "turns": [asdict(t) for t in turns],
            "stats": [asdict(s) for s in stats],
        }
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        print(f"\nWrote {len(turns)} turns to {args.out}")


if __name__ == "__main__":
    main()
