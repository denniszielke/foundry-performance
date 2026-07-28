"""Run the hosted weather agent (responses variation) locally and smoke-test it.

Starts ``src.hosted_agent_responses.agent`` as a local subprocess — no
container, no Foundry hosting — talking to the **real** Foundry project,
model deployment and toolbox from ``.env``. Without ``FOUNDRY_HOSTING_ENVIRONMENT``
set, ``azure-ai-agentserver-core`` resolves ``is_hosted=False``, so it skips
the hosted-only protocol-version check and uses local in-memory conversation
storage instead of the Foundry storage provider.

The container's managed identity is what normally gets toolbox access
(``scripts.ensure_toolbox_role``, applied by the deploy scripts) — your local
``az login`` identity does not have it by default, so this script also grants
the signed-in user the same ``Foundry User`` role before starting the server
(idempotent; see ``scripts._helpers.ensure_toolbox_role_for_signed_in_user``).

Usage::

    python -m scripts.run_hosted_agent_responses_local
    python -m scripts.run_hosted_agent_responses_local --query "weather in Zurich?"
    python -m scripts.run_hosted_agent_responses_local --keep-running
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

import httpx

from scripts._helpers import ROOT, env, ensure_toolbox_role_for_signed_in_user, load_env

DEFAULT_PORT = 8088
DEFAULT_QUERY = "What is the current weather in Berlin?"
READINESS_TIMEOUT_S = 60


def _wait_for_readiness(base_url: str, timeout_s: int) -> None:
    deadline = time.time() + timeout_s
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            resp = httpx.get(f"{base_url}/readiness", timeout=5.0)
            if resp.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(1)
    raise SystemExit(f"Agent never became ready at {base_url}/readiness (last error: {last_error})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default=DEFAULT_QUERY, help="Test message to send once the agent is up.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Local port to serve on (default 8088).")
    parser.add_argument(
        "--keep-running", action="store_true",
        help="Leave the local server running after the smoke test (Ctrl+C to stop it).",
    )
    args = parser.parse_args(argv)

    load_env()

    print("==> Granting local toolbox access (signed-in az CLI user)")
    ensure_toolbox_role_for_signed_in_user()

    env("AZURE_AI_PROJECT_ENDPOINT", required=True)  # fail fast with a clear message

    child_env = dict(os.environ)
    child_env.pop("FOUNDRY_HOSTING_ENVIRONMENT", None)  # keep is_hosted=False locally
    child_env["PORT"] = str(args.port)
    base_url = f"http://127.0.0.1:{args.port}"

    print(f"==> Starting src.hosted_agent_responses.agent on {base_url}")
    proc = subprocess.Popen(
        [sys.executable, "-m", "src.hosted_agent_responses.agent"],
        cwd=ROOT,
        env=child_env,
    )

    try:
        print("==> Waiting for readiness…")
        _wait_for_readiness(base_url, READINESS_TIMEOUT_S)

        print(f"==> Sending test query: {args.query!r}")
        # Reuse the same client the benchmark harness drives against deployed agents.
        from src.clients.common import Timer
        from src.clients.protocols import ResponsesClient

        async def _call() -> tuple[Timer, str]:
            async with ResponsesClient(base_url) as client:
                timer = Timer()
                text = await client.call(timer, args.query)
                return timer, text

        import asyncio

        timer, text = asyncio.run(_call())
        print(f"\n--- response ({timer.elapsed_s:.2f}s, ttfb={timer.ttfb_s}) ---\n{text}\n")

        if args.keep_running:
            print(f"Agent still running at {base_url} (pid {proc.pid}). Press Ctrl+C to stop it.")
            proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
