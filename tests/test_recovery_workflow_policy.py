import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CLUSTER_CONTROL = (REPO_ROOT / "cluster-control.sh").read_text()
AWAIT_PLAYBOOK = (REPO_ROOT / "workdir" / "playbooks" / "24-await-recovery-node.yml").read_text()
MASTER_RECOVERY_PLAYBOOK = (
    REPO_ROOT / "workdir" / "playbooks" / "21-recover-control-plane-node.yml"
).read_text()
BUNDLE_TASKS = (
    REPO_ROOT / "workdir" / "roles" / "control_plane_recovery_bundle" / "tasks" / "main.yml"
).read_text()
ANSIBLE_CFG = (REPO_ROOT / "workdir" / "ansible.cfg").read_text()


class RecoveryWorkflowPolicyTests(unittest.TestCase):
    def test_cluster_control_exposes_recovery_await_entrypoint(self):
        self.assertIn("--await-recovery-node", CLUSTER_CONTROL)
        self.assertIn("playbooks/24-await-recovery-node.yml", CLUSTER_CONTROL)

    def test_await_playbook_waits_for_cloud_init_and_records_report(self):
        self.assertIn("wait_for_connection", AWAIT_PLAYBOOK)
        self.assertIn("cloud-init", AWAIT_PLAYBOOK)
        self.assertIn("--wait", AWAIT_PLAYBOOK)
        self.assertIn("recovery-await", AWAIT_PLAYBOOK)

    def test_recovery_playbooks_prune_stale_known_hosts_entries(self):
        self.assertIn("ssh-keygen", AWAIT_PLAYBOOK)
        self.assertIn("ssh-keygen", MASTER_RECOVERY_PLAYBOOK)
        self.assertIn("recovery_known_hosts_path", MASTER_RECOVERY_PLAYBOOK)

    def test_recovery_bundle_manifest_captures_control_plane_network_facts(self):
        self.assertIn('"control_plane_interface_name"', BUNDLE_TASKS)
        self.assertIn('"control_plane_interface_mac"', BUNDLE_TASKS)
        self.assertIn('"control_plane_gateway"', BUNDLE_TASKS)
        self.assertIn('"control_plane_nameservers"', BUNDLE_TASKS)

    def test_ansible_ssh_defaults_use_accept_new_known_hosts_policy(self):
        self.assertIn("StrictHostKeyChecking=accept-new", ANSIBLE_CFG)
        self.assertIn("UserKnownHostsFile=/home/ansible/.ssh/known_hosts", ANSIBLE_CFG)


if __name__ == "__main__":
    unittest.main()
