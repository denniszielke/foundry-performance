"""Call the hosted hypothesis agent to plan, approve, reject, revise, or inspect."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

import httpx

from scripts._helpers import env, load_env
from src.clients.auth import EntraTokenAuth


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    plan = subparsers.add_parser("plan")
    plan.add_argument("scenario")
    plan.add_argument("--workflow-id")
    plan.add_argument("--comment")

    decide = subparsers.add_parser("decide")
    decide.add_argument("workflow_id")
    decide.add_argument("plan_revision", type=int)
    decide.add_argument("plan_digest")
    decide.add_argument("decision", choices=("approved", "rejected", "revise"))
    decide.add_argument("--comment")

    status = subparsers.add_parser("status")
    status.add_argument("workflow_id")
    return parser


async def _run(args: argparse.Namespace) -> None:
    load_env()
    project = env("AZURE_AI_PROJECT_ENDPOINT", required=True).rstrip("/")
    agent_name = env("HYPOTHESIS_HOSTED_AGENT_NAME", "scenario-hosted-hypothesis-agent")
    url = f"{project}/agents/{agent_name}/endpoint/protocols/invocations"
    request_id = uuid.uuid4().hex
    if args.action == "plan":
        body = {
            "action": "plan",
            "workflow_id": args.workflow_id,
            "scenario": args.scenario,
            "comment": args.comment,
            "client_request_id": request_id,
        }
    elif args.action == "decide":
        body = {
            "action": "approve",
            "workflow_id": args.workflow_id,
            "plan_revision": args.plan_revision,
            "plan_digest": args.plan_digest,
            "decision": args.decision,
            "comment": args.comment,
            "client_request_id": request_id,
        }
    else:
        body = {
            "action": "status",
            "workflow_id": args.workflow_id,
            "client_request_id": request_id,
        }
    async with httpx.AsyncClient(auth=EntraTokenAuth(), timeout=300.0) as client:
        response = await client.post(url, params={"api-version": "v1"}, json=body)
        response.raise_for_status()
        print(json.dumps(response.json(), indent=2))


def main() -> None:
    asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    main()