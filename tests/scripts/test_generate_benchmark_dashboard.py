import unittest

from scripts.generate_benchmark_dashboard import _quality_summary


class QualitySummaryTests(unittest.TestCase):
    def test_counts_failed_turns_once_at_run_level(self) -> None:
        quality = _quality_summary(
            [{"ok": True, "text": "sunny"}, {"ok": False, "error": "HTTP 404"}],
            [{"n": 2, "err": 1}],
        )

        self.assertEqual(quality["errors"], 1)

    def test_uses_aggregate_errors_when_raw_turns_are_missing(self) -> None:
        quality = _quality_summary([], [{"n": 10, "err": 3}, {"n": 10, "err": 2}])

        self.assertEqual(quality["errors"], 5)


if __name__ == "__main__":
    unittest.main()