import unittest

from scripts.run_benchmark_sandbox import (
    BatchRun,
    agent_egress_hosts,
    benchmark_command,
    build_matrix,
    egress_host_patterns,
    repo_tarball_url,
    resolve_agents,
    sandbox_env_file,
)

KNOWN = {
    "prompt": {"protocols": ("responses", "a2a", "invocations"), "default_auth": "entra"},
    "custom-maf": {"protocols": ("responses", "a2a", "invocations", "invocations_ws"), "default_auth": "none"},
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
        self.assertIn("codeload.github.com", patterns)
        self.assertIn("acct.services.ai.azure.com", patterns)
        self.assertIn("proxy.internal", patterns)
        self.assertNotIn("login.microsoftonline.com", patterns)
        self.assertEqual(len(patterns), len(set(patterns)))

    def test_identity_hosts_are_added_when_the_sandbox_authenticates(self) -> None:
        patterns = egress_host_patterns({}, (), include_identity=True)
        self.assertIn("login.microsoftonline.com", patterns)


class BootstrapTests(unittest.TestCase):
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
