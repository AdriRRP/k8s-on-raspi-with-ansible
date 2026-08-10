import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "workdir" / "tools" / "release_catalog_audit.py"
YAML_AVAILABLE = importlib.util.find_spec("yaml") is not None

if YAML_AVAILABLE:
    SPEC = importlib.util.spec_from_file_location("release_catalog_audit", MODULE_PATH)
    release_catalog_audit = importlib.util.module_from_spec(SPEC)
    assert SPEC.loader is not None
    sys.modules[SPEC.name] = release_catalog_audit
    SPEC.loader.exec_module(release_catalog_audit)
else:
    release_catalog_audit = None


@unittest.skipUnless(YAML_AVAILABLE, "PyYAML is required to import release_catalog_audit.py")
class ReleaseCatalogAuditTests(unittest.TestCase):
    def test_normalize_version_handles_tags_and_digest_pinned_images(self):
        self.assertEqual(release_catalog_audit.normalize_version("v1.36.3\n"), "1.36.3")
        self.assertEqual(
            release_catalog_audit.normalize_version("registry.example/app:v3.13.2@sha256:abc"),
            "3.13.2",
        )

    def test_latest_release_uses_highest_stable_semantic_version(self):
        releases = [
            {"tag_name": "chart-1.2.3", "draft": False, "prerelease": False},
            {"tag_name": "v2.0.0-rc.1", "draft": False, "prerelease": True},
            {"tag_name": "v1.2.3", "draft": False, "prerelease": False},
            {"tag_name": "v1.10.0", "draft": False, "prerelease": False},
        ]
        with patch.object(
            release_catalog_audit,
            "request",
            return_value=release_catalog_audit.json.dumps(releases).encode(),
        ):
            actual = release_catalog_audit.latest_github_release(
                "owner/repo", r"^v?\d+\.\d+\.\d+$", 1.0
            )

        self.assertEqual(actual, "v1.10.0")

    def test_latest_tag_supports_projects_with_chart_only_releases(self):
        tags = [{"name": "v0.16.0"}, {"name": "v0.16.1"}, {"name": "chart-0.17.0"}]
        with patch.object(
            release_catalog_audit,
            "request",
            return_value=release_catalog_audit.json.dumps(tags).encode(),
        ):
            actual = release_catalog_audit.latest_github_tag(
                "owner/repo", r"^v?\d+\.\d+\.\d+$", 1.0
            )

        self.assertEqual(actual, "v0.16.1")


if __name__ == "__main__":
    unittest.main()
