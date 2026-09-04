import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from scripts.run_benchmark_sandbox import (
    BatchRun,
    agent_egress_hosts,
    benchmark_command,
    bootstrap_sandbox,
    build_matrix,
    download_results,
    egress_host_patterns,
    repo_tarball_url,
    resolve_agents,
    run_benchmark_detached,
    sandbox_env_file,
    parse_args,
    write_report,
)

KNOWN = {
    "prompt": {"protocols": ("responses", "a2a", "invocations"), "default_auth": "entra"},
    "custom-maf": {"protocols": ("responses", "a2a", "invocations", "invocations_ws"), "default_auth": "none"},
}
HOSTED_KNOWN = {
    "hosted-responses": {"protocols": ("responses",), "default_auth": "entra"},
}


class ResolveAgentsTests(unittest.TestCase):
    def test_all_expands_to_every_known_agent(self) -> None:
        self.assertEqual(resolve_agents("all", KNOWN), ["custom-maf", "prompt"])

    def test_explicit_list_is_preserved(self) -> None:
        self.assertEqual(resolve_agents("prompt, custom-maf", KNOWN), ["prompt", "custom-maf"])

    def test_unknown_agent_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            resolve_agents("prompt,nope", KNOWN)


class BuildMatrixTests(unittest.TestCase):
    def test_hosted_agents_expand_to_dedicated_and_shared_runs(self) -> None:
        matrix = build_matrix(["hosted-responses"], "all", HOSTED_KNOWN)
        self.assertEqual([item.session_mode for item in matrix], ["dedicated", "shared"])
        self.assertEqual(
            [item.result_file for item in matrix],
            ["benchmark-hosted-responses-dedicated.json", "benchmark-hosted-responses-shared.json"],
        )

    def test_all_protocols_uses_what_each_agent_supports(self) -> None:
        matrix = build_matrix(["prompt", "custom-maf"], "all", KNOWN)
        self.assertEqual([item.agent for item in matrix], ["prompt", "custom-maf"])
        self.assertEqual(matrix[0].protocols, KNOWN["prompt"]["protocols"])

    def test_requested_protocols_are_intersected_per_agent(self) -> None:
        matrix = build_matrix(["prompt", "custom-maf"], "responses,invocations_ws", KNOWN)
        self.assertEqual(matrix[0].protocols, ("responses",))
        self.assertEqual(matrix[1].protocols, ("responses", "invocations_ws"))

    def test_agents_without_any_requested_protocol_are_skipped(self) -> None:
        matrix = build_matrix(["prompt", "custom-maf"], "invocations_ws", KNOWN)
        self.assertEqual([item.agent for item in matrix], ["custom-maf"])

    def test_empty_matrix_is_an_error(self) -> None:
        with self.assertRaises(SystemExit):
            build_matrix(["prompt"], "invocations_ws", KNOWN)


class CommandTests(unittest.TestCase):
    def test_hosted_command_carries_session_mode(self) -> None:
        command = benchmark_command(
            BatchRun(
                agent="hosted-responses",
                protocols=("responses",),
                result_file="benchmark-hosted-responses-shared.json",
                session_mode="shared",
            ),
            model_hosting="foundry",
            iterations=10,
            query="weather?",
            auth="auto",
        )
        self.assertIn("--session-mode shared", command)

    @patch("scripts.run_benchmark_sandbox.time.sleep")
    def test_detached_benchmark_polls_and_returns_log(self, _sleep) -> None:
        class Sandbox:
            def __init__(self):
                self.status_calls = 0

            def exec(self, command, *, working_directory):
                if "nohup sh -c" in command:
                    return SimpleNamespace(exit_code=0, stdout="", stderr="")
                self.status_calls += 1
                value = "RUNNING" if self.status_calls == 1 else "0"
                return SimpleNamespace(exit_code=0, stdout=value, stderr="")

            def read_file(self, path):
                return b"benchmark complete\n"

        result = run_benchmark_detached(Sandbox(), "python3 benchmark.py", label="custom-maf", timeout=30)

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "benchmark complete\n")

    def test_exec_timeout_defaults_to_one_hour(self) -> None:
        argv = ["runner", "--agents", "prompt", "--model-hosting", "foundry"]
        with patch("sys.argv", argv):
            args = parse_args(["prompt"])

        self.assertEqual(args.exec_timeout, 3600)

    def test_exec_timeout_can_be_overridden(self) -> None:
        argv = [
            "runner", "--agents", "prompt", "--model-hosting", "foundry",
            "--exec-timeout", "7200",
        ]
        with patch("sys.argv", argv):
            args = parse_args(["prompt"])

        self.assertEqual(args.exec_timeout, 7200)

    def test_benchmark_command_carries_every_flag(self) -> None:
        command = benchmark_command(
            BatchRun(agent="prompt", protocols=("responses", "a2a"), result_file="benchmark-prompt.json"),
            model_hosting="foundry",
            iterations=25,
            query="What is the current weather in Berlin?",
            auth="none",
        )
        self.assertIn("--agent prompt", command)
        self.assertIn("--protocols responses,a2a", command)
        self.assertIn("--model-hosting foundry", command)
        self.assertIn("--iterations 25", command)
        self.assertIn("--auth none", command)
        self.assertIn("/work/results/benchmark-prompt.json", command)
        self.assertIn("PYTHONPATH=/work/.python-packages python3", command)
        # The free-text query must not be able to break out of the command.
        self.assertIn("'What is the current weather in Berlin?'", command)


class EgressTests(unittest.TestCase):
    def test_agent_hosts_are_derived_from_env(self) -> None:
        values = {
            "AZURE_AI_PROJECT_ENDPOINT": "https://acct.services.ai.azure.com/api/projects/p",
            "WEATHER_CUSTOM_AGENT_MAF_URL": "https://maf.happysky.northcentralus.azurecontainerapps.io",
            "WEATHER_CUSTOM_AGENT_LANGCHAIN_URL": "",
        }
        self.assertEqual(
            agent_egress_hosts(values),
            ["acct.services.ai.azure.com", "maf.happysky.northcentralus.azurecontainerapps.io"],
        )

    def test_patterns_include_bootstrap_hosts_and_extras_without_duplicates(self) -> None:
        values = {"AZURE_AI_PROJECT_ENDPOINT": "https://acct.services.ai.azure.com/api/projects/p"}
        patterns = egress_host_patterns(values, ("pypi.org", "proxy.internal"), include_identity=False)
        self.assertIn("pypi.org", patterns)
        self.assertIn("bootstrap.pypa.io", patterns)
        self.assertIn("codeload.github.com", patterns)
        self.assertIn("acct.services.ai.azure.com", patterns)
        self.assertIn("proxy.internal", patterns)
        self.assertNotIn("login.microsoftonline.com", patterns)
        self.assertEqual(len(patterns), len(set(patterns)))

    def test_identity_hosts_are_added_when_the_sandbox_authenticates(self) -> None:
        patterns = egress_host_patterns({}, (), include_identity=True)
        self.assertIn("login.microsoftonline.com", patterns)


class BootstrapTests(unittest.TestCase):
    def test_download_results_recovers_available_files(self) -> None:
        matrix = [
            BatchRun(agent="custom-maf", protocols=("responses",), result_file="custom.json"),
            BatchRun(agent="prompt", protocols=("responses",), result_file="prompt.json"),
        ]

        class Sandbox:
            def list_files(self, path):
                return SimpleNamespace(entries=[SimpleNamespace(name="custom.json")])

            def read_file(self, path):
                return b'{"results": []}\n'

        with TemporaryDirectory() as directory:
            destination = Path(directory)
            download_results(Sandbox(), matrix, destination)

            self.assertEqual((destination / "custom.json").read_bytes(), b'{"results": []}\n')
            self.assertEqual(matrix[0].downloaded, str(destination / "custom.json"))
            self.assertIsNone(matrix[1].downloaded)

    def test_write_report_includes_every_batch_benchmark_file(self) -> None:
        run = {
            "datetime": "2026-09-04T12:00:00Z",
            "agent-type": "placeholder",
            "model-hosting": "foundry",
            "model-deployment": "test-model",
            "results": [],
        }
        with TemporaryDirectory() as directory:
            destination = Path(directory)
            for agent in ("custom-maf", "prompt"):
                payload = {**run, "agent-type": agent}
                (destination / f"benchmark-{agent}.json").write_text(json.dumps(payload), encoding="utf-8")
            (destination / "batch-summary.json").write_text("{}", encoding="utf-8")

            report = write_report(destination)
            html = report.read_text(encoding="utf-8")

            self.assertIn('"agent-type":"custom-maf"', html)
            self.assertIn('"agent-type":"prompt"', html)
            self.assertNotIn('"source":"batch-summary.json"', html)

    def test_bootstrap_creates_workdir_from_root(self) -> None:
        calls = []

        class Sandbox:
            def exec(self, command, *, working_directory):
                calls.append((command, working_directory))
                return SimpleNamespace(exit_code=0, stdout="", stderr="")

            def write_file(self, path, content):
                pass

        bootstrap_sandbox(Sandbox(), tarball_url="https://example.test/repo.tar.gz", env_file="")

        self.assertEqual(calls[0][1], "/")
        self.assertEqual(calls[1][1], "/work")
        self.assertIn("bootstrap.pypa.io/pip/pip.pyz", calls[2][0])
        self.assertIn("--target /work/.python-packages", calls[2][0])
        self.assertNotIn("-m venv", calls[2][0])

    def test_tarball_url_from_https_remote(self) -> None:
        self.assertEqual(
            repo_tarball_url("https://github.com/denniszielke/foundry-performance.git", "main"),
            "https://codeload.github.com/denniszielke/foundry-performance/tar.gz/main",
        )

    def test_tarball_url_rejects_non_repository_urls(self) -> None:
        with self.assertRaises(SystemExit):
            repo_tarball_url("https://github.com/denniszielke", "main")

    def test_env_file_only_forwards_known_keys(self) -> None:
        content = sandbox_env_file(
            {
                "AZURE_AI_PROJECT_ENDPOINT": "https://acct.services.ai.azure.com/api/projects/p",
                "AZURE_SUBSCRIPTION_ID": "should-not-be-forwarded",
                "WEATHER_CUSTOM_AGENT_MAF_URL": "",
            },
            None,
        )
        self.assertIn("AZURE_AI_PROJECT_ENDPOINT=", content)
        self.assertNotIn("AZURE_SUBSCRIPTION_ID", content)
        self.assertNotIn("WEATHER_CUSTOM_AGENT_MAF_URL", content)

    def test_env_file_includes_the_token_when_the_vnet_route_is_used(self) -> None:
        content = sandbox_env_file({"AZURE_AI_PROJECT_ENDPOINT": "https://acct.services.ai.azure.com"}, "tok")
        self.assertIn("AZURE_AI_ACCESS_TOKEN=tok", content)


if __name__ == "__main__":
    unittest.main()
