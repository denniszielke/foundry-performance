"""Benchmark runner for the weather agents.

Drives one or more protocols against a running agent endpoint and reports
latency, time-to-first-token (TTFB) and cold-vs-warm-vs-followup behaviour so
the three agent variations can be compared apples-to-apples.

Examples
--------
Benchmark every protocol of the custom agent (Container App)::

    python -m src.clients.run_benchmark --base-url https://weather-custom.<region>.azurecontainerapps.io

Only responses + websocket, 20 iterations, save raw results::

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
import uuid
from dataclasses import asdict

from src.clients.common import Stat, Timer, Turn, format_table, summarize
from src.clients.protocols import CLIENTS, ResponsesClient

DEFAULT_QUERY = "What is the current weather in Berlin?"
FOLLOWUP_QUERY = "And what about the forecast for the next three days there?"


async def _timed(client, query: str, session_id: str | None) -> tuple[Timer, str]:
    timer = Timer()
    text = await client.call(timer, query, session_id)
    return timer, text


async def _bench_protocol(name: str, base_url: str, model: str, iterations: int, query: str) -> list[Turn]:
    cls = CLIENTS[name]
    turns: list[Turn] = []
    make = (lambda: cls(base_url, model)) if cls is ResponsesClient else (lambda: cls(base_url))

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


async def run(base_url: str, protocols: list[str], model: str, iterations: int, query: str) -> list[Turn]:
    turns: list[Turn] = []
    for name in protocols:
        print(f"→ benchmarking {name} ...", flush=True)
        turns.extend(await _bench_protocol(name, base_url, model, iterations, query))
    return turns


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark the weather agent across protocols.")
    parser.add_argument("--base-url", required=True, help="Agent base URL, e.g. http://127.0.0.1:8088")
    parser.add_argument(
        "--protocols",
        default="all",
        help=f"Comma-separated subset of {sorted(CLIENTS)} or 'all'.",
    )
    parser.add_argument("--iterations", type=int, default=10, help="Warm/followup iterations per protocol.")
    parser.add_argument("--model", default="weather-agent", help="Model/agent name for the responses protocol.")
    parser.add_argument("--query", default=DEFAULT_QUERY, help="Question to ask each turn.")
    parser.add_argument("--out", default=None, help="Optional path to write raw turns + stats as JSON.")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.protocols == "all":
        protocols = list(CLIENTS)
    else:
        protocols = [p.strip() for p in args.protocols.split(",") if p.strip()]
        unknown = [p for p in protocols if p not in CLIENTS]
        if unknown:
            raise SystemExit(f"Unknown protocol(s): {unknown}. Available: {sorted(CLIENTS)}")

    turns = asyncio.run(run(args.base_url, protocols, args.model, args.iterations, args.query))
    stats = summarize(turns)

    print()
    print(format_table(stats))

    if args.out:
        payload = {
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
