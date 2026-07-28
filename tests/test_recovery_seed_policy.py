import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
GROUP_VARS = (REPO_ROOT / "workdir" / "inventory" / "group_vars" / "all.yml").read_text()
MASTER_PLAYBOOK = (
    REPO_ROOT / "workdir" / "playbooks" / "22-render-master-recovery-seed.yml"
).read_text()
NETWORK_TEMPLATE = (
    REPO_ROOT / "workdir" / "roles" / "recovery_cloud_init" / "templates" / "network-config.j2"
).read_text()
USER_DATA_TEMPLATE = (
    REPO_ROOT / "workdir" / "roles" / "recovery_cloud_init" / "templates" / "user-data.j2"
).read_text()


class RecoverySeedPolicyTests(unittest.TestCase):
    def test_recovery_seed_defaults_disable_first_boot_package_update(self):
        self.assertIn("recovery_seed_cloud_init_package_update: false", GROUP_VARS)
        self.assertIn(
            "package_update: {{ recovery_seed_cloud_init_package_update | bool | to_json }}",
            USER_DATA_TEMPLATE,
        )

    def test_master_recovery_seed_is_rendered_with_static_networking(self):
        self.assertIn("recovery_seed_network_mode: static", MASTER_PLAYBOOK)
        self.assertIn("recovery_seed_network_required: true", MASTER_PLAYBOOK)

    def test_network_template_requires_network_for_static_nodes(self):
        static_section = "{% if recovery_seed_network_mode == 'static' %}\n    dhcp4: false\n"
        self.assertIn(static_section, NETWORK_TEMPLATE)
        self.assertIn("    optional: false", NETWORK_TEMPLATE)

    def test_network_template_supports_mac_matching_for_control_plane(self):
        self.assertIn('macaddress: "{{ recovery_seed_interface_mac }}"', NETWORK_TEMPLATE)
        self.assertIn('name: "{{ recovery_seed_interface_name }}"', NETWORK_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
