import importlib.util
import pathlib
import unittest

MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "workdir"
    / "tools"
    / "upgrade_to_latest_stable.py"
)

YAML_AVAILABLE = importlib.util.find_spec("yaml") is not None

if YAML_AVAILABLE:
    SPEC = importlib.util.spec_from_file_location("upgrade_to_latest_stable", MODULE_PATH)
    upgrade_to_latest_stable = importlib.util.module_from_spec(SPEC)
    assert SPEC.loader is not None
    SPEC.loader.exec_module(upgrade_to_latest_stable)
else:
    upgrade_to_latest_stable = None


@unittest.skipUnless(YAML_AVAILABLE, "PyYAML is required to import upgrade_to_latest_stable.py")
class ResolveResumableKubernetesHopsTests(unittest.TestCase):
    def test_returns_same_minor_patch_upgrade(self):
        hops = upgrade_to_latest_stable.resolve_resumable_kubernetes_hops(
            ["1.36.2"],
            "1.36.3",
            {},
        )

        self.assertEqual(hops, ["1.36.3"])

    def test_rejects_same_minor_downgrade(self):
        with self.assertRaisesRegex(RuntimeError, "downgrades"):
            upgrade_to_latest_stable.resolve_resumable_kubernetes_hops(
                ["1.36.3"],
                "1.36.2",
                {},
            )

    def test_returns_longest_route_for_mixed_cluster(self):
        mapping = {
            "1.32": "1.33.13",
            "1.33": "1.34.10",
            "1.34": "1.35.7",
            "1.35": "1.36.3",
        }

        hops = upgrade_to_latest_stable.resolve_resumable_kubernetes_hops(
            ["1.32.13", "1.33.13"],
            "1.36.3",
            mapping,
        )

        self.assertEqual(hops, ["1.33.13", "1.34.10", "1.35.7", "1.36.3"])

    def test_accepts_nodes_already_on_target(self):
        mapping = {
            "1.35": "1.36.3",
        }

        hops = upgrade_to_latest_stable.resolve_resumable_kubernetes_hops(
            ["1.35.7", "1.36.3"],
            "1.36.3",
            mapping,
        )

        self.assertEqual(hops, ["1.36.3"])

    def test_routes_current_catalog_release_to_kubernetes_137(self):
        mapping = {
            "1.35": "1.36.3",
            "1.36": "1.37.0",
        }

        hops = upgrade_to_latest_stable.resolve_resumable_kubernetes_hops(
            ["1.35.7", "1.36.3"],
            "1.37.0",
            mapping,
        )

        self.assertEqual(hops, ["1.36.3", "1.37.0"])

    def test_detects_cycles_in_kubernetes_route(self):
        mapping = {
            "1.32": "1.33.13",
            "1.33": "1.32.9",
        }

        with self.assertRaisesRegex(RuntimeError, "ciclo"):
            upgrade_to_latest_stable.resolve_resumable_kubernetes_hops(
                ["1.32.13"],
                "1.34.9",
                mapping,
            )


@unittest.skipUnless(YAML_AVAILABLE, "PyYAML is required to import upgrade_to_latest_stable.py")
class ResolveKubernetesDebRevisionTests(unittest.TestCase):
    def test_prefers_version_specific_deb_revision(self):
        catalog = {
            "kubernetes": {
                "deb_revision": "1.1",
                "deb_revisions_by_version": {
                    "1.36.2": "2.1",
                    "1.36.3": "1.1",
                },
            }
        }

        revision = upgrade_to_latest_stable.resolve_kubernetes_deb_revision(
            "1.36.3",
            catalog,
        )

        self.assertEqual(revision, "1.1")

    def test_falls_back_to_default_deb_revision(self):
        catalog = {
            "kubernetes": {
                "deb_revision": "1.1",
                "deb_revisions_by_version": {
                    "1.36.2": "2.1",
                },
            }
        }

        revision = upgrade_to_latest_stable.resolve_kubernetes_deb_revision(
            "1.35.6",
            catalog,
        )

        self.assertEqual(revision, "1.1")


@unittest.skipUnless(YAML_AVAILABLE, "PyYAML is required to import upgrade_to_latest_stable.py")
class NormalizeUbuntuVersionTests(unittest.TestCase):
    def test_normalizes_point_release_to_upgrade_series(self):
        self.assertEqual(
            upgrade_to_latest_stable.normalize_ubuntu_version("Ubuntu 24.04.3 LTS"),
            "24.04",
        )

    def test_accepts_release_without_point_version(self):
        self.assertEqual(
            upgrade_to_latest_stable.normalize_ubuntu_version("Ubuntu 26.04 LTS"),
            "26.04",
        )

    def test_rejects_unrecognized_os_image(self):
        self.assertEqual(
            upgrade_to_latest_stable.normalize_ubuntu_version("Debian GNU/Linux 13"),
            "",
        )


if __name__ == "__main__":
    unittest.main()
