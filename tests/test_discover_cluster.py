import importlib.util
import pathlib
import unittest

MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "workdir" / "inventory" / "discover_cluster.py"
)
SPEC = importlib.util.spec_from_file_location("discover_cluster", MODULE_PATH)
discover_cluster = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(discover_cluster)


class DiscoverySafetyTests(unittest.TestCase):
    def test_accepts_bounded_private_ipv4_network(self):
        network = discover_cluster.validate_scan_network("192.168.0.0/24")

        self.assertEqual(str(network), "192.168.0.0/24")

    def test_rejects_large_network(self):
        with self.assertRaisesRegex(RuntimeError, "safety limit"):
            discover_cluster.validate_scan_network("10.0.0.0/8")

    def test_rejects_public_network(self):
        with self.assertRaisesRegex(RuntimeError, "non-private"):
            discover_cluster.validate_scan_network("8.8.8.0/24")

    def test_rejects_ipv6_network(self):
        with self.assertRaisesRegex(RuntimeError, "IPv4"):
            discover_cluster.validate_scan_network("fd00::/120")


if __name__ == "__main__":
    unittest.main()
