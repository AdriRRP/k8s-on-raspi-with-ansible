import pathlib
import re
import subprocess
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class ProjectPolicyTests(unittest.TestCase):
    def test_docker_context_excludes_runtime_secrets(self):
        dockerignore = (REPO_ROOT / ".dockerignore").read_text()

        self.assertTrue(dockerignore.startswith("*\n"))
        self.assertNotIn("!config", dockerignore)

    def test_control_image_verifies_kubectl_and_pins_python_tools(self):
        dockerfile = (REPO_ROOT / "Dockerfile").read_text()
        group_vars = (REPO_ROOT / "workdir" / "inventory" / "group_vars" / "all.yml").read_text()
        requirements = (REPO_ROOT / "workdir" / "requirements-control.txt").read_text()
        lock = (REPO_ROOT / "workdir" / "requirements-control-lock.txt").read_text()

        base_image = re.fullmatch(
            r"FROM ubuntu:(\d+\.\d+)@sha256:[a-f0-9]{64}",
            dockerfile.splitlines()[0],
        )
        install_version = re.search(
            r'^\s+install_version: "(\d+\.\d+)"$',
            group_vars,
            re.MULTILINE,
        )
        self.assertIsNotNone(base_image)
        self.assertIsNotNone(install_version)
        self.assertEqual(base_image.group(1), install_version.group(1))
        self.assertIn("kubectl.sha256", dockerfile)
        self.assertIn("sha256sum --check --strict", dockerfile)
        self.assertIn("ansible-core==", requirements)
        self.assertIn("ansible-lint==", requirements)
        self.assertIn("ruff==", requirements)
        self.assertIn("requirements-control-lock.txt", dockerfile)
        self.assertTrue(
            all("==" in line for line in lock.splitlines() if line and not line.startswith("#"))
        )
        direct_requirements = {
            line for line in requirements.splitlines() if line and not line.startswith("#")
        }
        self.assertTrue(direct_requirements.issubset(set(lock.splitlines())))
        self.assertNotIn("NOPASSWD", dockerfile)

    def test_shell_wrapper_is_location_independent_and_parallel_safe(self):
        wrapper = (REPO_ROOT / "cluster-control.sh").read_text()

        self.assertIn('dirname -- "${BASH_SOURCE[0]}"', wrapper)
        self.assertIn("--validate", wrapper)
        self.assertIn("--help", wrapper)
        self.assertNotIn('--name "${SCRIPT_NAME}"', wrapper)

    def test_shell_wrapper_rejects_ambiguous_options_before_docker(self):
        wrapper = REPO_ROOT / "cluster-control.sh"
        cases = (
            ("--dry-run",),
            ("--upgrade-plan", "--dry-run", "--apply-upgrade"),
            ("--build", "--generate-key"),
        )

        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [str(wrapper), *arguments],
                    cwd="/tmp",
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 2)

    def test_ci_actions_are_pinned_to_full_commit_shas(self):
        workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
        action_references = re.findall(r"uses:\s+[^@\s]+@([^\s]+)", workflow)

        self.assertGreaterEqual(len(action_references), 3)
        self.assertTrue(all(re.fullmatch(r"[a-f0-9]{40}", ref) for ref in action_references))
        self.assertIn("./tools/validate.sh", workflow)

    def test_all_ansible_yaml_documents_have_document_start(self):
        missing = []
        for path in (REPO_ROOT / "workdir").rglob("*"):
            if path.is_file() and path.suffix in {".yml", ".yaml"}:
                if not path.read_text().startswith("---"):
                    missing.append(str(path.relative_to(REPO_ROOT)))

        self.assertEqual(missing, [])

    def test_static_inventory_exposes_control_plane_compatibility_group(self):
        for inventory_name in ("hosts.ini", "bootstrap.ini"):
            inventory = (REPO_ROOT / "workdir" / "inventory" / inventory_name).read_text()
            self.assertIn("[control_plane:children]", inventory)

    def test_cluster_upgrades_default_to_dry_run(self):
        group_vars = (REPO_ROOT / "workdir" / "inventory" / "group_vars" / "all.yml").read_text()
        upgrade_tool = (REPO_ROOT / "workdir" / "tools" / "upgrade_to_latest_stable.py").read_text()

        self.assertIn("upgrade_execution_mode: dry-run", group_vars)
        self.assertIn("upgrade_force_drain: false", group_vars)
        self.assertIn('extra_vars.get("upgrade_execution_mode", "dry-run")', upgrade_tool)

    def test_runtime_artifacts_are_versioned_and_checksum_verified(self):
        defaults = (
            REPO_ROOT / "workdir" / "roles" / "containerd" / "defaults" / "main.yml"
        ).read_text()
        tasks = (REPO_ROOT / "workdir" / "roles" / "containerd" / "tasks" / "main.yml").read_text()

        self.assertNotIn("containerd/containerd/main/containerd.service", defaults)
        self.assertGreaterEqual(tasks.count("checksum:"), 4)
        self.assertIn("containerd_checksums[arch]", tasks)
        self.assertIn("runc_checksums[arch]", tasks)
        self.assertIn("cni_plugins_checksums[arch]", tasks)


if __name__ == "__main__":
    unittest.main()
