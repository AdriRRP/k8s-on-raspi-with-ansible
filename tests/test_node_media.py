import importlib.util
import pathlib
import unittest

MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "tools" / "node_media.py"

SPEC = importlib.util.spec_from_file_location("node_media", MODULE_PATH)
node_media = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(node_media)


class ParseSha256SumsTests(unittest.TestCase):
    def test_returns_checksum_for_matching_filename(self):
        checksum = node_media.parse_sha256sums(
            "abcd1234 *ubuntu.img.xz\nffffeeee *other.img.xz\n",
            "ubuntu.img.xz",
        )

        self.assertEqual(checksum, "abcd1234")

    def test_raises_when_filename_is_missing(self):
        with self.assertRaisesRegex(ValueError, "Could not find checksum"):
            node_media.parse_sha256sums(
                "abcd1234 *ubuntu.img.xz\n",
                "missing.img.xz",
            )


class DiskCandidateTests(unittest.TestCase):
    def test_external_usb_disk_is_candidate(self):
        record = {
            "whole_disk": True,
            "writable_media": True,
            "internal": False,
            "virtual_or_physical": "Physical",
            "removable": True,
            "removable_media": True,
            "ejectable": True,
        }

        self.assertTrue(node_media.is_safe_candidate(record))

    def test_internal_disk_is_rejected(self):
        record = {
            "whole_disk": True,
            "writable_media": True,
            "internal": True,
            "virtual_or_physical": "Physical",
            "removable": False,
            "removable_media": False,
            "ejectable": False,
        }

        self.assertFalse(node_media.is_safe_candidate(record))


if __name__ == "__main__":
    unittest.main()
