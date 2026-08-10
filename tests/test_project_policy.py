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
        self.assertIn("--baseline", wrapper)
        self.assertIn("--benchmark", wrapper)
        self.assertIn("--performance-profile", wrapper)
        self.assertIn("--node-local-dns", wrapper)
        self.assertIn("--reconcile-node-hygiene", wrapper)
        self.assertIn("--release-audit", wrapper)
        self.assertIn("--help", wrapper)
        self.assertNotIn('--name "${SCRIPT_NAME}"', wrapper)
        self.assertNotIn("-b|--build", wrapper)

    def test_baseline_is_observe_only_and_writes_below_runtime_outputs(self):
        group_vars = (REPO_ROOT / "workdir" / "inventory" / "group_vars" / "all.yml").read_text()
        playbook = (
            REPO_ROOT / "workdir" / "playbooks" / "25-capture-performance-baseline.yml"
        ).read_text()

        self.assertIn("performance_baseline_profile: observe", group_vars)
        self.assertIn("{{ kubernetes_outputs }}/benchmarks", group_vars)
        self.assertIn("performance_baseline_profile == 'observe'", playbook)
        self.assertNotIn("kubernetes.core.k8s:", playbook)

    def test_active_benchmark_is_explicit_bounded_and_always_cleans_up(self):
        tool = (REPO_ROOT / "workdir" / "tools" / "cluster_benchmark.py").read_text()
        playbook = (
            REPO_ROOT / "workdir" / "playbooks" / "27-run-performance-benchmark.yml"
        ).read_text()

        self.assertIn("performance_benchmark_profile == 'active-safe'", playbook)
        self.assertIn("finally:", tool)
        self.assertIn('"delete",\n                "namespace"', tool)
        self.assertIn("Benchmark namespace cleanup failed", tool)
        self.assertIn("bounded_integer(1, 64)", tool)
        self.assertIn("node-role.kubernetes.io/control-plane", tool)
        self.assertIn("compare_with_control", tool)
        self.assertNotIn("/dev/sd", tool)

    def test_performance_profiles_are_serial_and_have_control_rollback(self):
        playbook = (
            REPO_ROOT / "workdir" / "playbooks" / "28-apply-performance-profile.yml"
        ).read_text()
        role = (
            REPO_ROOT / "workdir" / "roles" / "performance_tuning" / "tasks" / "main.yml"
        ).read_text()

        self.assertIn("serial: 1", playbook)
        self.assertIn("any_errors_fatal: true", playbook)
        self.assertIn("'control'", role)
        self.assertIn("performance_tuning_profile == 'control'", role)
        self.assertIn("--check", role)

    def test_node_local_dns_defaults_to_audit_and_pins_multiarch_image(self):
        group_vars = (REPO_ROOT / "workdir" / "inventory" / "group_vars" / "all.yml").read_text()
        tasks = (
            REPO_ROOT / "workdir" / "roles" / "node_local_dns" / "tasks" / "main.yml"
        ).read_text()
        playbook = (
            REPO_ROOT / "workdir" / "playbooks" / "29-manage-node-local-dns.yml"
        ).read_text()

        self.assertIn("node_local_dns_state: audit", group_vars)
        self.assertRegex(
            group_vars,
            r"k8s-dns-node-cache:1[.]26[.]8@sha256:[a-f0-9]{64}",
        )
        self.assertIn("node_local_dns_kube_proxy_mode == 'iptables'", tasks)
        self.assertIn("node_local_dns_state == 'absent'", tasks)
        self.assertIn("Verify DNS resolution after state transition", tasks)
        self.assertIn("rescue:", playbook)
        self.assertIn("node_local_dns_state: absent", playbook)

    def test_prometheus_verifies_optional_node_local_dns_metrics(self):
        tasks = (REPO_ROOT / "workdir" / "roles" / "prometheus" / "tasks" / "main.yml").read_text()

        self.assertIn("Detect optional NodeLocal DNS deployment", tasks)
        self.assertIn('up{job="node-local-dns"}', tasks)
        self.assertIn("prometheus_node_local_dns.resources | length > 0", tasks)

    def test_prometheus_normalizes_and_verifies_lens_helm_labels(self):
        config = (
            REPO_ROOT
            / "workdir"
            / "roles"
            / "prometheus"
            / "templates"
            / "prometheus-configmap.yml.j2"
        ).read_text()
        tasks = (
            REPO_ROOT / "workdir" / "roles" / "observability_verify" / "tasks" / "main.yml"
        ).read_text()

        self.assertIn("target_label: node", config)
        self.assertIn("target_label: kubernetes_node", config)
        self.assertIn("target_label: instance", config)
        self.assertIn("Verify Prometheus labels required by Lens Helm queries", tasks)
        self.assertIn("always:", tasks)
        self.assertIn('node_cpu_seconds_total{node=~".+"}', tasks)
        self.assertIn('container_cpu_usage_seconds_total{instance=~".+",pod=~".+"}', tasks)

    def test_metrics_server_is_pinned_hardened_and_runtime_verified(self):
        group_vars = (REPO_ROOT / "workdir" / "inventory" / "group_vars" / "all.yml").read_text()
        manifest = (
            REPO_ROOT
            / "workdir"
            / "roles"
            / "metrics_server"
            / "templates"
            / "metrics-server.yml.j2"
        ).read_text()
        tasks = (
            REPO_ROOT / "workdir" / "roles" / "metrics_server" / "tasks" / "main.yml"
        ).read_text()
        monitoring = (REPO_ROOT / "workdir" / "playbooks" / "13-setup-monitoring.yml").read_text()
        upgrades = (
            REPO_ROOT / "workdir" / "roles" / "k8s_upgrade_addons" / "tasks" / "main.yml"
        ).read_text()

        self.assertRegex(
            group_vars,
            r"metrics-server:v0[.]9[.]0@sha256:[a-f0-9]{64}",
        )
        self.assertRegex(
            group_vars,
            r"metrics-server:v0[.]8[.]1@sha256:[a-f0-9]{64}",
        )
        self.assertIn("readOnlyRootFilesystem: true", manifest)
        self.assertIn("allowPrivilegeEscalation: false", manifest)
        self.assertIn("medium: Memory", manifest)
        self.assertIn("metrics_server_kubelet_insecure_tls", manifest)
        self.assertIn("startupProbe:", manifest)
        self.assertIn("timeoutSeconds: {{ metrics_server_probe_timeout_seconds }}", manifest)
        self.assertIn("Wait until the Kubernetes Metrics API is available", tasks)
        self.assertIn("kubectl", tasks)
        self.assertIn("top", tasks)
        self.assertIn("- metrics_server", monitoring)
        self.assertIn("Reconcile installed Metrics Server", upgrades)

    def test_hygiene_reconciler_only_includes_bounded_task_files(self):
        playbook = (
            REPO_ROOT / "workdir" / "playbooks" / "26-reconcile-node-hygiene.yml"
        ).read_text()

        self.assertEqual(playbook.count("ansible.builtin.include_role:"), 2)
        self.assertIn("tasks_from: housekeeping", playbook)
        self.assertIn("tasks_from: iptables", playbook)
        self.assertNotIn("\n  roles:", playbook)

    def test_cluster_verification_uses_no_undeclared_jq_binary(self):
        network_check = (
            REPO_ROOT / "workdir" / "roles" / "k8s_verify" / "tasks" / "network-check.yml"
        ).read_text()

        self.assertNotIn("jq ", network_check)
        self.assertNotIn("json_query", network_check)
        self.assertIn("jsonpath={.status.numberReady}", network_check)

    def test_upgrade_roles_drop_become_when_delegating_to_control_host(self):
        for role in ("k8s_upgrade_control_plane", "k8s_upgrade_workers"):
            with self.subTest(role=role):
                tasks = (REPO_ROOT / "workdir" / "roles" / role / "tasks" / "main.yml").read_text()
                self.assertNotRegex(
                    tasks,
                    r"delegate_to: localhost\n(?!\s+become: false)",
                )

    def test_metallb_probe_requests_an_existing_echo_resource(self):
        echo_test = (
            REPO_ROOT / "workdir" / "roles" / "metallb" / "tasks" / "echo-test.yml"
        ).read_text()

        self.assertIn('"http://{{ echo_service_ip.stdout }}/hostname"', echo_test)

    def test_kube_state_metrics_uses_a_numeric_non_root_identity(self):
        deployment = (
            REPO_ROOT
            / "workdir"
            / "roles"
            / "kube_state_metrics"
            / "templates"
            / "ksm-deployment.yml.j2"
        ).read_text()

        self.assertIn("runAsNonRoot: true", deployment)
        self.assertIn("runAsUser: 65534", deployment)
        self.assertIn("runAsGroup: 65534", deployment)

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
        self.assertIn("schedule:", workflow)
        self.assertIn("release_catalog_audit.py", workflow)

    def test_direct_workload_images_are_pinned_by_multiarch_digest(self):
        group_vars = (REPO_ROOT / "workdir" / "inventory" / "group_vars" / "all.yml").read_text()
        direct_images = re.findall(
            r"^\s+(?:prometheus|grafana|node_exporter|nfs_provisioner|registry|smoke_test)_image:"
            r"\s*>-\n\s+(\S+)",
            group_vars,
            re.MULTILINE,
        )

        self.assertEqual(len(direct_images), 6)
        self.assertTrue(
            all(re.search(r":[^@\s]+@sha256:[a-f0-9]{64}$", image) for image in direct_images)
        )
        kube_state_metrics = re.findall(
            r"kube-state-metrics:[^\s]+@sha256:[a-f0-9]{64}",
            group_vars,
        )
        self.assertEqual(len(kube_state_metrics), 6)

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
        self.assertIn("upgrade_reconcile_runtime: true", group_vars)
        self.assertIn('extra_vars.get("upgrade_execution_mode", "dry-run")', upgrade_tool)
        self.assertIn("if not os_hops and not kubernetes_hops:", upgrade_tool)
        self.assertIn('"upgrade_os_release_nodes": "false"', upgrade_tool)
        self.assertIn('"upgrade_os_patch_nodes",', upgrade_tool)

        for role in ("k8s_upgrade_control_plane", "k8s_upgrade_workers"):
            tasks = (REPO_ROOT / "workdir" / "roles" / role / "tasks" / "main.yml").read_text()
            self.assertIn("name: containerd", tasks)
            self.assertIn("ansible.builtin.meta: flush_handlers", tasks)
            self.assertIn("failed_when: expected_containerd_version not in", tasks)
            self.assertIn("failed_when: expected_runc_version not in", tasks)

    def test_runtime_reconciliation_supports_slow_pi_startup_and_legacy_registry(self):
        grafana_defaults = (
            REPO_ROOT / "workdir" / "roles" / "grafana" / "defaults" / "main.yml"
        ).read_text()
        addon_tasks = (
            REPO_ROOT / "workdir" / "roles" / "k8s_upgrade_addons" / "tasks" / "main.yml"
        ).read_text()
        grafana_config = (
            REPO_ROOT
            / "workdir"
            / "roles"
            / "grafana"
            / "templates"
            / "grafana-configmap-ini.yml.j2"
        ).read_text()

        self.assertIn("grafana_rollout_retries: 72", grafana_defaults)
        self.assertIn("grafana_startup_probe_failure_threshold: 60", grafana_defaults)
        self.assertIn("upgrade_registry_existing_namespace", addon_tasks)
        self.assertIn('namespace: "{{ item }}"', addon_tasks)
        self.assertIn('name: "{{ registry_name }}"', addon_tasks)
        self.assertIn("check_for_updates = false", grafana_config)
        self.assertIn("check_for_plugin_updates = false", grafana_config)
        self.assertIn("preinstall_disabled = true", grafana_config)

    def test_nfs_smoke_test_uses_delete_reclaim_policy(self):
        verify_tasks = (
            REPO_ROOT / "workdir" / "roles" / "nfs_provisioner" / "tasks" / "verify.yml"
        ).read_text()

        self.assertIn("nfs_test_storage_class_name", verify_tasks)
        self.assertIn("reclaimPolicy: Delete", verify_tasks)
        self.assertIn('archiveOnDelete: "false"', verify_tasks)
        self.assertIn("Wait for ephemeral test volume deletion", verify_tasks)

    def test_registry_is_lightweight_persistent_and_safely_migrated(self):
        group_vars = (REPO_ROOT / "workdir" / "inventory" / "group_vars" / "all.yml").read_text()
        registry_root = REPO_ROOT / "workdir" / "roles" / "registry"
        defaults = (registry_root / "defaults" / "main.yml").read_text()
        main_tasks = (registry_root / "tasks" / "main.yml").read_text()
        legacy_tasks = (registry_root / "tasks" / "legacy.yml").read_text()
        deployment = (registry_root / "templates" / "registry-deployment.yml.j2").read_text()
        config = (registry_root / "templates" / "registry-configmap.yml.j2").read_text()

        self.assertIn("ghcr.io/project-zot/zot:v2.1.20@sha256:", group_vars)
        self.assertIn("registry_allow_nonempty_legacy_migration: false", defaults)
        self.assertIn("registry_allow_insecure_external_exposure: false", defaults)
        self.assertIn("Refuse implicit migration of non-empty legacy registries", legacy_tasks)
        self.assertLess(
            main_tasks.index("Include registry verification tasks"),
            main_tasks.index("Remove empty legacy registry"),
        )
        self.assertIn("runAsNonRoot: true", deployment)
        self.assertIn("readOnlyRootFilesystem: true", deployment)
        self.assertIn('"commit": true', config)
        self.assertIn('"dedupe": false', config)
        self.assertIn('"gc": true', config)
        self.assertIn('"scrub"', config)
        self.assertIn('"tls"', config)
        self.assertIn('"htpasswd"', config)
        self.assertIn('"accessControl"', config)
        self.assertIn('"adminPolicy"', config)
        self.assertNotIn('"search"', config)
        self.assertNotIn('"ui"', config)
        verify_tasks = (registry_root / "tasks" / "verify.yml").read_text()
        self.assertIn("failed_when: wget_test.rc != 0", verify_tasks)
        self.assertNotIn("--no-check-certificate", verify_tasks)
        self.assertIn("Wait until previous registry test pod is absent", verify_tasks)
        prometheus_tasks = (
            REPO_ROOT / "workdir" / "roles" / "prometheus" / "tasks" / "main.yml"
        ).read_text()
        self.assertIn("Verify Prometheus is scraping Zot when installed", prometheus_tasks)
        self.assertIn('up{job="zot-registry"}', prometheus_tasks)

    def test_registry_external_access_is_tls_authenticated_and_trusted(self):
        group_vars = (REPO_ROOT / "workdir" / "inventory" / "group_vars" / "all.yml").read_text()
        registry_root = REPO_ROOT / "workdir" / "roles" / "registry"
        pki_tasks = (registry_root / "tasks" / "pki.yml").read_text()
        service = (registry_root / "templates" / "registry-service.yml.j2").read_text()
        client_tasks = (
            REPO_ROOT / "workdir" / "roles" / "registry_client" / "tasks" / "main.yml"
        ).read_text()

        self.assertIn("registry_service_type: LoadBalancer", group_vars)
        self.assertIn('registry_lb_ip: "192.168.0.240"', group_vars)
        self.assertIn("registry_tls_enabled: true", group_vars)
        self.assertIn("registry_certificate_renewal_days: 30", group_vars)
        self.assertIn("htpasswd", pki_tasks)
        self.assertIn("-cbB", pki_tasks)
        self.assertIn("subjectAltName={{", pki_tasks)
        self.assertIn("ca_path", (registry_root / "tasks" / "external-verify.yml").read_text())
        self.assertNotIn(
            "'{}' not in external_response.content",
            (registry_root / "tasks" / "external-verify.yml").read_text(),
        )
        self.assertIn("registry_external_port", service)
        self.assertIn(
            "/etc/containerd/certs.d",
            (
                REPO_ROOT / "workdir" / "roles" / "registry_client" / "defaults" / "main.yml"
            ).read_text(),
        )
        self.assertIn("Install registry CA", client_tasks)

    def test_storage_hygiene_is_allowlisted_and_opt_in(self):
        defaults = (
            REPO_ROOT / "workdir" / "roles" / "storage_hygiene" / "defaults" / "main.yml"
        ).read_text()
        tasks = (
            REPO_ROOT / "workdir" / "roles" / "storage_hygiene" / "tasks" / "main.yml"
        ).read_text()

        self.assertIn("storage_hygiene_apply: false", defaults)
        self.assertIn("storage_hygiene_retired_claims:", defaults)
        self.assertIn("item.status.phase | default('') == 'Released'", tasks)
        self.assertIn("Archive retired PV metadata", tasks)
        self.assertIn("previous non-empty forensic report was preserved", tasks)
        self.assertGreaterEqual(tasks.count("when: storage_hygiene_apply | bool"), 2)

    def test_lightweight_availability_and_maintenance_gates(self):
        metrics_defaults = (
            REPO_ROOT / "workdir" / "roles" / "metrics_server" / "defaults" / "main.yml"
        ).read_text()
        metrics_manifest = (
            REPO_ROOT
            / "workdir"
            / "roles"
            / "metrics_server"
            / "templates"
            / "metrics-server.yml.j2"
        ).read_text()
        nfs_tasks = (
            REPO_ROOT / "workdir" / "roles" / "nfs_provisioner" / "tasks" / "create.yml"
        ).read_text()
        maintenance = (
            REPO_ROOT / "workdir" / "roles" / "maintenance_audit" / "tasks" / "main.yml"
        ).read_text()

        self.assertIn("metrics_server_replicas: 2", metrics_defaults)
        self.assertIn("kind: PodDisruptionBudget", metrics_manifest)
        self.assertIn("requiredDuringSchedulingIgnoredDuringExecution", metrics_manifest)
        self.assertIn("ENABLE_LEADER_ELECTION", nfs_tasks)
        self.assertIn("nfs_provisioner_replicas", nfs_tasks)
        self.assertIn("apt-get", maintenance)
        self.assertIn("/var/run/reboot-required", maintenance)
        self.assertIn("/proc/pressure/io", maintenance)

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

    def test_upgrade_reconciliation_detects_namespaced_registry(self):
        tasks = (
            REPO_ROOT / "workdir" / "roles" / "k8s_upgrade_addons" / "tasks" / "main.yml"
        ).read_text()
        defaults = (
            REPO_ROOT / "workdir" / "roles" / "k8s_upgrade_addons" / "defaults" / "main.yml"
        ).read_text()

        self.assertIn('namespace: "{{ item }}"', tasks)
        self.assertIn('name: "{{ registry_name }}"', tasks)
        self.assertIn("registry_legacy_namespaces", tasks)
        self.assertIn("upgrade_registry_deployments", tasks)
        self.assertIn("- kube-system", defaults)

    def test_upgrade_snapshot_path_is_frozen_before_capture_and_read(self):
        tasks = (
            REPO_ROOT / "workdir" / "roles" / "k8s_upgrade_snapshot" / "tasks" / "main.yml"
        ).read_text()

        self.assertIn("k8s_upgrade_snapshot_target_file_resolved", tasks)
        self.assertIn("Verify captured snapshot exists", tasks)
        self.assertNotIn('src: "{{ k8s_upgrade_snapshot_target_file }}"', tasks)


if __name__ == "__main__":
    unittest.main()
