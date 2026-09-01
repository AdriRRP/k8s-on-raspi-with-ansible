import importlib.util
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "workdir" / "tools" / "cluster_upgrade_report.py"

SPEC = importlib.util.spec_from_file_location("cluster_upgrade_report", MODULE_PATH)
cluster_upgrade_report = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(cluster_upgrade_report)


class ExtractNodeSummaryTests(unittest.TestCase):
    def test_extracts_only_roles_from_the_exact_kubernetes_label_domain(self):
        node = {
            "metadata": {
                "name": "raspi-master",
                "labels": {
                    "node-role.kubernetes.io/control-plane": "",
                    "node-role.kubernetes.io/worker": "",
                    "node-role.kubernetes.io.evil/attacker": "",
                    "evilnode-role.kubernetes.io/attacker": "",
                },
            },
            "status": {},
        }

        summary = cluster_upgrade_report.extract_node_summary(node)

        self.assertEqual(summary["roles"], ["control-plane", "worker"])

    def test_empty_role_name_retains_control_plane_compatibility(self):
        node = {
            "metadata": {
                "name": "raspi-master",
                "labels": {"node-role.kubernetes.io/": ""},
            },
            "status": {},
        }

        summary = cluster_upgrade_report.extract_node_summary(node)

        self.assertEqual(summary["roles"], ["control-plane"])


if __name__ == "__main__":
    unittest.main()
