"""Approve and execute the exact plan saved by test_hypothesis_plan."""

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

DEFAULT_INPUT = ROOT / "results" / "hypothesis-plan-test.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--comment", default="Approved by the deployed-agent test script.")
    return parser


def _endpoint() -> str:
    project = env("AZURE_AI_PROJECT_ENDPOINT", required=True).rstrip("/")
    agent_name = env("HYPOTHESIS_HOSTED_AGENT_NAME", "scenario-hosted-hypothesis-agent")
    return f"{project}/agents/{agent_name}/endpoint/protocols/invocations"


def _load_plan(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"plan file does not exist: {path}; run test_hypothesis_plan first")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("plan file must contain a JSON object")
    required = ("workflow_id", "plan_revision", "plan_digest")
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise RuntimeError(f"plan file is missing: {', '.join(missing)}")
    return payload


async def _run(args: argparse.Namespace) -> None:
    load_env()
    plan = _load_plan(args.input)
    body = {
        "action": "approve",
        "workflow_id": plan["workflow_id"],
        "plan_revision": plan["plan_revision"],
        "plan_digest": plan["plan_digest"],
        "decision": "approved",
        "comment": args.comment,
        "client_request_id": f"approve-test-{uuid.uuid4().hex}",
    }
    async with httpx.AsyncClient(auth=EntraTokenAuth(), timeout=600.0) as client:
        response = await client.post(_endpoint(), params={"api-version": "v1"}, json=body)
    if response.is_error:
        raise RuntimeError(f"approval request failed ({response.status_code}): {response.text}")
    payload = response.json()
    print(json.dumps(payload, indent=2))
    if payload.get("status") != "completed":
        raise RuntimeError(f"approval did not complete execution; status={payload.get('status')!r}")


def main() -> None:
    asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    main()