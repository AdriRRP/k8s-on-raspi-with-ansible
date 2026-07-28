import importlib.util
import json
import pathlib
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


cluster_baseline = load_module(
    "cluster_baseline",
    REPO_ROOT / "workdir" / "tools" / "cluster_baseline.py",
)
node_baseline = load_module(
    "node_baseline",
    REPO_ROOT / "workdir" / "tools" / "node_baseline.py",
)


class BaselineMathTests(unittest.TestCase):
    def test_percentile_uses_nearest_rank(self):
        self.assertEqual(cluster_baseline.percentile([5, 1, 4, 2, 3], 95), 5)

    def test_latency_summary_ignores_failed_samples(self):
        summary = cluster_baseline.latency_summary(
            [
                {"ok": True, "latency_ms": 10},
                {"ok": False, "latency_ms": 500},
                {"ok": True, "latency_ms": 20},
            ]
        )

        self.assertEqual(summary["successful_samples"], 2)
        self.assertEqual(summary["median_ms"], 15)
        self.assertEqual(summary["maximum_ms"], 20)

    def test_delta_percent_handles_zero_baseline(self):
        self.assertIsNone(cluster_baseline.delta_percent(10, 0))
        self.assertEqual(cluster_baseline.delta_percent(110, 100), 10)

    def test_failed_unit_names_extracts_unit_column(self):
        units = cluster_baseline.failed_unit_names(
            [
                "logrotate.service loaded failed failed Rotate log files",
                "netfilter-persistent.service loaded failed failed netfilter persistent",
            ]
        )

        self.assertEqual(
            units,
            ["logrotate.service", "netfilter-persistent.service"],
        )

    def test_format_bytes_uses_binary_units(self):
        self.assertEqual(cluster_baseline.format_bytes(1024**3), "1.0 GiB")


class NodeParsingTests(unittest.TestCase):
    def test_parse_pressure(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as handle:
            handle.write("some avg10=1.25 avg60=0.50 avg300=0.20 total=42\n")
            handle.flush()
            pressure = node_baseline.parse_pressure(handle.name)

        self.assertEqual(pressure["some"]["avg10"], 1.25)
        self.assertEqual(pressure["some"]["total"], 42)


class BaselineReportTests(unittest.TestCase):
    def test_report_emits_findings_and_markdown(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = pathlib.Path(directory) / "20260728T120000Z"
            nodes_dir = run_dir / "nodes"
            nodes_dir.mkdir(parents=True)
            cluster = {
                "schema_version": 1,
                "profile": "observe",
                "captured_at": "2026-07-28T12:00:00Z",
                "repository_revision": "abc123",
                "expected_kubernetes_version": "1.36.3",
                "version": {"serverVersion": {"gitVersion": "v1.36.3"}},
                "nodes": [{"name": "worker1", "ready": True}],
                "pods": [{"phase": "Running", "restarts": 0}],
                "api_readyz_latency": {
                    "summary": {
                        "samples": 3,
                        "successful_samples": 3,
                        "minimum_ms": 10,
                        "median_ms": 12,
                        "p95_ms": 15,
                        "p99_ms": 15,
                        "maximum_ms": 15,
                    }
                },
            }
            node = {
                "node": {
                    "architecture": "arm64",
                    "kernel": "6.8.0",
                    "uptime_seconds": 100,
                },
                "cpu": {
                    "logical_cpu_count": 4,
                    "load_average": {"one_minute": 1},
                    "temperatures": [{"celsius": 55}],
                    "raspberry_pi_throttling": {"stdout": "throttled=0x0"},
                },
                "memory": {
                    "total_bytes": 8_000,
                    "available_bytes": 4_000,
                    "swap_total_bytes": 0,
                },
                "pressure": {
                    "cpu": {"some": {"avg10": 0}},
                    "memory": {"full": {"avg10": 0}},
                    "io": {"full": {"avg10": 0}},
                },
                "storage": {
                    "filesystems": [
                        {
                            "mountpoint": "/",
                            "used_percent": 20,
                            "available_bytes": 100,
                        }
                    ],
                    "path_sizes": {},
                },
                "services": {
                    "states": {"containerd": "active", "kubelet": "active"},
                    "failed_units": [],
                },
            }
            (run_dir / "cluster.json").write_text(json.dumps(cluster), encoding="utf-8")
            (nodes_dir / "worker1.json").write_text(json.dumps(node), encoding="utf-8")

            summary, report = cluster_baseline.build_report(run_dir)

            self.assertEqual(summary["findings"], [])
            self.assertTrue(report.is_file())
            self.assertIn("Cluster performance baseline", report.read_text())
            self.assertIn("Cumulative Pod restarts", report.read_text())


if __name__ == "__main__":
    unittest.main()
