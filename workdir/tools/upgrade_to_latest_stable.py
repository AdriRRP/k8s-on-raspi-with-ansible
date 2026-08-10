#!/usr/bin/env python3

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

DEFAULT_OUTPUT_DIR = Path("/home/ansible/.kube/outputs")
DEFAULT_DISCOVERY_OUTPUT = DEFAULT_OUTPUT_DIR / "discovered_inventory.json"
DEFAULT_PLAN_OUTPUT = DEFAULT_OUTPUT_DIR / "latest-stable-upgrade-plan.json"
DEFAULT_KUBECONFIG = "/home/ansible/.kube/config"
GROUP_VARS_PATH = Path("/home/ansible/workdir/inventory/group_vars/all.yml")
DISCOVERY_SCRIPT = Path("/home/ansible/workdir/inventory/discover_cluster.py")
UPGRADE_PLAYBOOK = Path("/home/ansible/workdir/playbooks/15-upgrade-cluster.yml")
RECONCILE_PLAYBOOK = Path("/home/ansible/workdir/playbooks/16-post-upgrade-reconcile.yml")


def run(command, check=True):
    return subprocess.run(command, check=check, text=True, capture_output=True)


def parse_extra_vars(argv):
    values = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "-e" and index + 1 < len(argv):
            payload = argv[index + 1]
            if "=" in payload:
                key, value = payload.split("=", 1)
                values[key] = value
            index += 2
            continue
        if token.startswith("-e") and "=" in token[2:]:
            key, value = token[2:].split("=", 1)
            values[key] = value
        index += 1
    return values


def as_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def normalize_ubuntu_version(os_image):
    if not os_image.startswith("Ubuntu "):
        return ""

    version = os_image.removeprefix("Ubuntu ").split()[0]
    parts = version.split(".")
    if len(parts) < 2 or not all(part.isdigit() for part in parts[:2]):
        return ""
    return ".".join(parts[:2])


def parse_kubernetes_version(version):
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise RuntimeError(f"Versión de Kubernetes no válida: {version}. Usa el formato X.Y.Z.")
    return tuple(int(part) for part in parts)


def load_catalog():
    payload = yaml.safe_load(GROUP_VARS_PATH.read_text(encoding="utf-8"))
    return payload["platform_release_catalog"], payload


def discover_cluster(write_path):
    command = [
        sys.executable,
        str(DISCOVERY_SCRIPT),
        "--list",
        "--write",
        str(write_path),
    ]
    run(command)


def get_cluster_state(kubeconfig_path):
    version_result = run(
        [
            "kubectl",
            "version",
            "--output=json",
            f"--kubeconfig={kubeconfig_path}",
        ]
    )
    nodes_result = run(
        [
            "kubectl",
            "get",
            "nodes",
            "-o",
            "json",
            f"--kubeconfig={kubeconfig_path}",
        ]
    )
    version_payload = json.loads(version_result.stdout)
    nodes_payload = json.loads(nodes_result.stdout)
    current_kubernetes_version = version_payload["serverVersion"]["gitVersion"].removeprefix("v")
    os_images = []
    os_versions = []
    node_kubernetes_versions = []
    for item in nodes_payload.get("items", []):
        os_image = item.get("status", {}).get("nodeInfo", {}).get("osImage", "")
        os_images.append(os_image)
        kubelet_version = item.get("status", {}).get("nodeInfo", {}).get("kubeletVersion", "")
        if kubelet_version:
            node_kubernetes_versions.append(kubelet_version.removeprefix("v"))
        os_versions.append(normalize_ubuntu_version(os_image))
    unique_os_versions = sorted({value for value in os_versions if value})
    unique_node_kubernetes_versions = sorted({value for value in node_kubernetes_versions if value})
    if any(not version for version in os_versions):
        raise RuntimeError(
            "El clúster contiene nodos sin una versión de Ubuntu reconocible. "
            f"OS images detectadas: {os_images}"
        )
    return {
        "current_kubernetes_version": current_kubernetes_version,
        "current_kubernetes_minor": ".".join(current_kubernetes_version.split(".")[:2]),
        "current_os_images": os_images,
        "current_os_version": (
            unique_os_versions[0]
            if len(unique_os_versions) == 1
            else f"mixed:{','.join(unique_os_versions)}"
        ),
        "current_os_versions": unique_os_versions,
        "node_kubernetes_versions": unique_node_kubernetes_versions,
    }


def resolve_hops(current_version, target_version, mapping, label):
    if not target_version or current_version == target_version:
        return []
    hops = []
    cursor = current_version
    visited = {cursor}
    while cursor != target_version:
        if cursor not in mapping:
            raise RuntimeError(
                f"No hay ruta soportada de {label} desde {cursor} hasta {target_version}."
            )
        cursor = mapping[cursor]
        if cursor in visited:
            raise RuntimeError(f"Se detectó un ciclo en la ruta de upgrade de {label}.")
        hops.append(cursor)
        visited.add(cursor)
        if len(hops) > 16:
            raise RuntimeError(f"La ruta de upgrade de {label} es sospechosamente larga.")
    return hops


def resolve_resumable_os_hops(current_versions, target_version, mapping):
    if not target_version:
        return []

    version_paths = {
        version: resolve_hops(version, target_version, mapping, "Ubuntu")
        for version in current_versions
    }
    longest_path = max(version_paths.values(), key=len, default=[])

    for version, path in version_paths.items():
        if not path:
            continue
        if longest_path[-len(path) :] != path:
            raise RuntimeError(
                "El clúster mezcla rutas de upgrade de Ubuntu incompatibles. "
                f"Versiones detectadas: {current_versions}. "
                f"La ruta para {version} es {path}, pero la ruta base es {longest_path}."
            )

    return longest_path


def resolve_kubernetes_hops(current_version, target_version, mapping):
    if not target_version or current_version == target_version:
        return []

    current_semver = parse_kubernetes_version(current_version)
    target_semver = parse_kubernetes_version(target_version)
    if target_semver < current_semver:
        raise RuntimeError(
            "Los downgrades de Kubernetes no están soportados: "
            f"{current_version} -> {target_version}."
        )

    current_minor = ".".join(current_version.split(".")[:2])
    target_minor = ".".join(target_version.split(".")[:2])
    if current_minor == target_minor:
        return [target_version]

    hops = []
    cursor_minor = current_minor
    visited = {cursor_minor}

    while cursor_minor != target_minor:
        if cursor_minor not in mapping:
            raise RuntimeError(
                f"No hay ruta soportada de Kubernetes desde {cursor_minor} hasta {target_minor}."
            )
        next_version = mapping[cursor_minor]
        next_minor = ".".join(next_version.split(".")[:2])
        if next_minor in visited:
            raise RuntimeError("Se detectó un ciclo en la ruta de upgrade de Kubernetes.")
        hops.append(next_version)
        visited.add(next_minor)
        cursor_minor = next_minor

    if hops:
        hops[-1] = target_version
    return hops


def resolve_resumable_kubernetes_hops(current_versions, target_version, mapping):
    if not target_version:
        return []

    version_paths = {
        version: resolve_kubernetes_hops(version, target_version, mapping)
        for version in current_versions
    }
    longest_path = max(version_paths.values(), key=len, default=[])

    for version, path in version_paths.items():
        if not path:
            continue
        if longest_path[-len(path) :] != path:
            raise RuntimeError(
                "El clúster mezcla rutas de upgrade de Kubernetes incompatibles. "
                f"Versiones detectadas: {current_versions}. "
                f"La ruta para {version} es {path}, pero la ruta base es {longest_path}."
            )

    return longest_path


def resolve_kubernetes_deb_revision(target_version, catalog):
    revisions = catalog["kubernetes"].get("deb_revisions_by_version", {})
    if target_version in revisions:
        return revisions[target_version]
    return catalog["kubernetes"]["deb_revision"]


def build_playbook_command(playbook, extra_vars):
    command = [
        "ansible-playbook",
        "-i",
        "inventory/discover_cluster.py",
        str(playbook),
    ]
    for key, value in extra_vars.items():
        command.extend(["-e", f"{key}={value}"])
    return command


def write_plan(plan):
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_PLAN_OUTPUT.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")


def print_plan(plan):
    print("Plan de upgrade estable generado:")
    print(json.dumps(plan, indent=2))


def main():
    extra_vars = parse_extra_vars(sys.argv[1:])
    execution_mode = extra_vars.get("upgrade_execution_mode", "dry-run")
    maintenance_scope = extra_vars.get("upgrade_maintenance_scope", "cluster")
    kubeconfig_path = extra_vars.get("kubeconfig_local_path", DEFAULT_KUBECONFIG)

    if maintenance_scope not in {"cluster", "kubernetes", "os"}:
        raise RuntimeError(
            f"maintenance_scope inválido: {maintenance_scope}. Usa cluster, kubernetes u os."
        )

    catalog, group_vars = load_catalog()
    discover_cluster(DEFAULT_DISCOVERY_OUTPUT)
    state = get_cluster_state(kubeconfig_path)

    os_target = extra_vars.get(
        "upgrade_target_os_version",
        catalog["os"]["latest_stable_lts_version"],
    )
    kubernetes_target = extra_vars.get(
        "upgrade_target_kubernetes_version",
        catalog["kubernetes"]["latest_upstream_version"],
    )

    os_hops = []
    kubernetes_hops = []
    if maintenance_scope in {"cluster", "os"}:
        os_hops = resolve_resumable_os_hops(
            state["current_os_versions"],
            os_target,
            catalog["os"]["supported_release_upgrade_targets"],
        )
    if maintenance_scope in {"cluster", "kubernetes"}:
        kubernetes_hops = resolve_resumable_kubernetes_hops(
            state["node_kubernetes_versions"] or [state["current_kubernetes_version"]],
            kubernetes_target,
            catalog["kubernetes"]["supported_upgrade_targets"],
        )

    plan = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "execution_mode": execution_mode,
        "maintenance_scope": maintenance_scope,
        "current_os_images": state["current_os_images"],
        "current_os_version": state["current_os_version"],
        "target_os_version": os_target if maintenance_scope in {"cluster", "os"} else "",
        "os_release_hops": os_hops,
        "current_kubernetes_version": state["current_kubernetes_version"],
        "current_node_kubernetes_versions": state["node_kubernetes_versions"],
        "target_kubernetes_version": (
            kubernetes_target if maintenance_scope in {"cluster", "kubernetes"} else ""
        ),
        "kubernetes_hops": kubernetes_hops,
        "discovery_output_path": str(DEFAULT_DISCOVERY_OUTPUT),
    }
    write_plan(plan)
    print_plan(plan)

    if execution_mode == "dry-run":
        print(f"Plan escrito en {DEFAULT_PLAN_OUTPUT}")
        return 0

    passthrough_vars = {
        key: value
        for key, value in extra_vars.items()
        if key
        in {
            "upgrade_target_kubernetes_deb_revision",
            "upgrade_os_patch_nodes",
            "upgrade_enforce_replicated_workloads",
            "upgrade_require_pdb_protection",
            "upgrade_allow_single_replica_workloads",
            "upgrade_reconcile_platform_addons",
            "upgrade_reconcile_runtime",
            "upgrade_delete_emptydir_data",
            "upgrade_force_drain",
            "upgrade_drain_timeout",
            "upgrade_drain_grace_period",
        }
    }

    if not os_hops and not kubernetes_hops:
        command = build_playbook_command(
            UPGRADE_PLAYBOOK,
            {
                **passthrough_vars,
                "upgrade_execution_mode": "apply",
                "upgrade_maintenance_scope": maintenance_scope,
                "upgrade_os_release_nodes": "false",
                "upgrade_target_os_version": os_target,
                "upgrade_target_kubernetes_version": kubernetes_target,
                "upgrade_target_kubernetes_deb_revision": resolve_kubernetes_deb_revision(
                    kubernetes_target, catalog
                ),
                "upgrade_reconcile_platform_addons": "true",
            },
        )
        print("Las versiones ya coinciden; ejecutando mantenimiento, runtime y reconciliado")
        subprocess.run(command, check=True, cwd="/home/ansible/workdir")
        return 0

    for hop in os_hops:
        command = build_playbook_command(
            UPGRADE_PLAYBOOK,
            {
                **passthrough_vars,
                "upgrade_execution_mode": "apply",
                "upgrade_maintenance_scope": "os",
                "upgrade_os_patch_nodes": "true",
                "upgrade_os_release_nodes": "true",
                "upgrade_target_os_version": hop,
                "upgrade_reconcile_platform_addons": "false",
            },
        )
        print(f"Ejecutando hop de Ubuntu hacia {hop}")
        subprocess.run(command, check=True, cwd="/home/ansible/workdir")

    for hop in kubernetes_hops:
        command = build_playbook_command(
            UPGRADE_PLAYBOOK,
            {
                **passthrough_vars,
                "upgrade_target_kubernetes_deb_revision": resolve_kubernetes_deb_revision(
                    hop, catalog
                ),
                "upgrade_execution_mode": "apply",
                "upgrade_maintenance_scope": "kubernetes",
                "upgrade_target_kubernetes_version": hop,
                "upgrade_reconcile_platform_addons": "true",
            },
        )
        print(f"Ejecutando hop de Kubernetes hacia {hop}")
        subprocess.run(command, check=True, cwd="/home/ansible/workdir")

    if os_hops and not kubernetes_hops:
        reconcile_command = build_playbook_command(
            RECONCILE_PLAYBOOK,
            {
                **passthrough_vars,
                "upgrade_reconcile_platform_addons": "true",
            },
        )
        print("Ejecutando reconcile final tras upgrade de Ubuntu")
        subprocess.run(reconcile_command, check=True, cwd="/home/ansible/workdir")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
