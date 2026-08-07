"""Submit a planning request to the deployed hypothesis agent and save its response."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import httpx

from scripts._helpers import ROOT, env, load_env
from src.clients.auth import EntraTokenAuth

DEFAULT_SCENARIO = (
    "Test the hypothesis that Seattle is currently warmer than Sydney. "
    "Create a verification plan using the available tools, but do not execute it until approved."
)
DEFAULT_OUTPUT = ROOT / "results" / "hypothesis-plan-test.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO)
    parser.add_argument("--comment", default="Planning test against the deployed agent.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def _endpoint() -> str:
    project = env("AZURE_AI_PROJECT_ENDPOINT", required=True).rstrip("/")
    agent_name = env("HYPOTHESIS_HOSTED_AGENT_NAME", "scenario-hosted-hypothesis-agent")
    return f"{project}/agents/{agent_name}/endpoint/protocols/invocations"


async def _invoke(body: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(auth=EntraTokenAuth(), timeout=300.0) as client:
        response = await client.post(_endpoint(), params={"api-version": "v1"}, json=body)
    if response.is_error:
        raise RuntimeError(f"planning request failed ({response.status_code}): {response.text}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("planning response was not a JSON object")
    return payload


async def _run(args: argparse.Namespace) -> None:
    load_env()
    payload = await _invoke(
        {
            "action": "plan",
            "scenario": args.scenario,
            "comment": args.comment,
            "client_request_id": f"plan-test-{uuid.uuid4().hex}",
        }
    )
    required = ("workflow_id", "plan_revision", "plan_digest")
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise RuntimeError(f"planning response is missing: {', '.join(missing)}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    print(f"\nSaved approval input to {args.output}")


def main() -> None:
    asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    main()