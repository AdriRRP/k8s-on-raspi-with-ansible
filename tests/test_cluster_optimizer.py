import importlib.util
import pathlib
import unittest
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


optimizer = load_module(
    "cluster_optimizer",
    REPO_ROOT / "workdir" / "tools" / "cluster_optimizer.py",
)


def benchmark(api, startup, dns, cpu, network):
    return {
        "results": {
            "api_ready_latency_ms": {"median": api},
            "pod_startup_latency_ms": {"median": startup},
            "dns": [{"average_ms": dns}, {"average_ms": dns}],
            "cpu_hash": [{"milliseconds": cpu}, {"milliseconds": cpu}],
            "network": [
                {
                    "source_node": "worker1",
                    "destination_node": "worker2",
                    "mib_per_second": network,
                }
            ],
        }
    }


class ClusterOptimizerTests(unittest.TestCase):
    def test_playbook_preserves_typed_extra_vars_as_json(self):
        with mock.patch.object(optimizer, "run") as run:
            optimizer.playbook(
                "example.yml",
                {"enabled": False, "sample_count": 3, "profile": "observe"},
            )

        command = run.call_args.args[0]
        self.assertEqual(command[-2], "--extra-vars")
        self.assertEqual(
            optimizer.json.loads(command[-1]),
            {"enabled": False, "sample_count": 3, "profile": "observe"},
        )

    def test_aggregate_uses_median_across_repeated_runs(self):
        result = optimizer.aggregate(
            [
                benchmark(10, 100, 2, 500, 80),
                benchmark(30, 300, 6, 700, 100),
                benchmark(20, 200, 4, 600, 90),
            ]
        )

        self.assertEqual(
            result,
            {"api": 20, "startup": 200, "dns": 4, "cpu": 600, "network": 90},
        )

    def test_node_local_dns_requires_primary_improvement(self):
        accepted, deltas, reasons = optimizer.evaluate(
            {"api": 10, "startup": 100, "dns": 10, "cpu": 100, "network": 100},
            {"api": 10, "startup": 100, "dns": 9.6, "cpu": 100, "network": 100},
            "node-local-dns",
            10,
            5,
        )

        self.assertFalse(accepted)
        self.assertEqual(deltas["dns"], -4)
        self.assertIn("required 5.00%", reasons[0])

    def test_schedutil_rejects_secondary_regression(self):
        accepted, _, reasons = optimizer.evaluate(
            {"api": 10, "startup": 100, "dns": 10, "cpu": 100, "network": 100},
            {"api": 12, "startup": 100, "dns": 10, "cpu": 90, "network": 100},
            "schedutil",
            10,
            5,
        )

        self.assertFalse(accepted)
        self.assertIn("api regressed", reasons[0])

    def test_baseline_warnings_block_mutation(self):
        blockers = optimizer.baseline_blockers(
            [
                {
                    "findings": [
                        {"severity": "warn", "scope": "worker1", "message": "throttled"},
                        {"severity": "info", "scope": "cluster", "message": "ok"},
                    ]
                }
            ]
        )

        self.assertEqual(len(blockers), 1)
        self.assertEqual(blockers[0]["scope"], "worker1")

    def test_postflight_rejects_health_findings_and_temperature_increase(self):
        before = [{"nodes": [{"maximum_temperature_celsius": 40.0}]}]
        after = {
            "nodes": [{"maximum_temperature_celsius": 46.0}],
            "findings": [
                {"severity": "warn", "scope": "master", "message": "under-voltage occurred"}
            ],
        }

        reasons = optimizer.postflight_reasons(before, after, 5.0)

        self.assertIn("postflight master: under-voltage occurred", reasons)
        self.assertIn("temperature increased by 6.00 C", reasons[1])

    def test_wait_for_pod_tolerates_transient_api_failure(self):
        benchmark_module = load_module(
            "cluster_benchmark_wait_test",
            REPO_ROOT / "workdir" / "tools" / "cluster_benchmark.py",
        )
        ready_pod = {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}
        responses = [
            SimpleNamespace(returncode=1, stdout="", stderr="temporary API failure"),
            SimpleNamespace(returncode=0, stdout=optimizer.json.dumps(ready_pod), stderr=""),
        ]

        with (
            mock.patch.object(benchmark_module, "kubectl", side_effect=responses),
            mock.patch.object(benchmark_module.time, "sleep"),
        ):
            result = benchmark_module.wait_for_pod("config", "namespace", "pod", timeout=1)

        self.assertEqual(result, ready_pod)


if __name__ == "__main__":
    unittest.main()
