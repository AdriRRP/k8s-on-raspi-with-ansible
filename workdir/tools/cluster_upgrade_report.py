#!/usr/bin/env python3

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime


def run_kubectl(kubeconfig, resource):
    result = subprocess.run(
        [
            "kubectl",
            "get",
            resource,
            "--all-namespaces",
            "--output=json",
            f"--kubeconfig={kubeconfig}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def run_kubectl_no_namespace(kubeconfig, resource):
    result = subprocess.run(
        [
            "kubectl",
            "get",
            resource,
            "--output=json",
            f"--kubeconfig={kubeconfig}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def normalize_selector(spec):
    selector = (spec or {}).get("selector", {})
    match_labels = selector.get("matchLabels") or {}
    match_expressions = selector.get("matchExpressions") or []
    return match_labels, match_expressions


def labels_match(labels, match_labels, match_expressions):
    if not match_labels and not match_expressions:
        return False

    for key, value in match_labels.items():
        if labels.get(key) != value:
            return False

    for expression in match_expressions:
        key = expression.get("key")
        operator = expression.get("operator")
        values = expression.get("values", [])
        present = key in labels
        current = labels.get(key)

        if operator == "In" and current not in values:
            return False
        if operator == "NotIn" and current in values:
            return False
        if operator == "Exists" and not present:
            return False
        if operator == "DoesNotExist" and present:
            return False
    return True


def extract_node_summary(node):
    metadata = node.get("metadata", {})
    status = node.get("status", {})
    labels = metadata.get("labels", {})
    node_info = status.get("nodeInfo", {})
    addresses = status.get("addresses", [])
    internal_ip = ""
    for address in addresses:
        if address.get("type") == "InternalIP":
            internal_ip = address.get("address", "")
            break

    roles = []
    for key in labels:
        if key.startswith("node-role.kubernetes.io/"):
            role_name = key.split("/", 1)[1] or "control-plane"
            roles.append(role_name)

    ready = False
    for condition in status.get("conditions", []):
        if condition.get("type") == "Ready" and condition.get("status") == "True":
            ready = True
            break

    return {
        "name": metadata.get("name"),
        "internal_ip": internal_ip,
        "roles": sorted(set(roles)),
        "ready": ready,
        "kubelet_version": node_info.get("kubeletVersion"),
        "kube_proxy_version": node_info.get("kubeProxyVersion"),
        "container_runtime_version": node_info.get("containerRuntimeVersion"),
        "os_image": node_info.get("osImage"),
        "kernel_version": node_info.get("kernelVersion"),
    }


def extract_pdb_summary(pdb):
    metadata = pdb.get("metadata", {})
    spec = pdb.get("spec", {})
    match_labels, match_expressions = normalize_selector(spec)
    return {
        "namespace": metadata.get("namespace"),
        "name": metadata.get("name"),
        "min_available": spec.get("minAvailable"),
        "max_unavailable": spec.get("maxUnavailable"),
        "match_labels": match_labels,
        "match_expressions": match_expressions,
    }


def workload_identifier(namespace, name):
    return f"{namespace}/{name}"


def extract_workload_summary(item, pdbs_by_namespace, exempt_namespaces, exempt_workloads):
    metadata = item.get("metadata", {})
    spec = item.get("spec", {})
    template = spec.get("template", {})
    template_labels = (template.get("metadata") or {}).get("labels") or {}
    namespace = metadata.get("namespace")
    name = metadata.get("name")
    replicas = spec.get("replicas", 1)
    matched_pdbs = []
    current_workload_identifier = workload_identifier(namespace, name)

    for pdb in pdbs_by_namespace.get(namespace, []):
        if labels_match(template_labels, pdb["match_labels"], pdb["match_expressions"]):
            matched_pdbs.append(pdb["name"])

    return {
        "kind": item.get("kind"),
        "namespace": namespace,
        "name": name,
        "workload_identifier": current_workload_identifier,
        "replicas": replicas,
        "template_labels": template_labels,
        "pdb_protected": len(matched_pdbs) > 0,
        "matched_pdbs": matched_pdbs,
        "single_replica": replicas < 2,
        "exempt_namespace": namespace in exempt_namespaces,
        "exempt_workload": current_workload_identifier in exempt_workloads,
    }


def build_report(kubeconfig, exempt_namespaces, exempt_workloads):
    version = run_kubectl_no_namespace(kubeconfig, "nodes")
    deployments = run_kubectl(kubeconfig, "deployments")
    statefulsets = run_kubectl(kubeconfig, "statefulsets")
    pod_disruption_budgets = run_kubectl(kubeconfig, "poddisruptionbudgets.policy")
    version_info = subprocess.run(
        [
            "kubectl",
            "version",
            "--output=json",
            f"--kubeconfig={kubeconfig}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    version_payload = json.loads(version_info.stdout)

    node_items = [extract_node_summary(item) for item in version.get("items", [])]
    pdb_items = [extract_pdb_summary(item) for item in pod_disruption_budgets.get("items", [])]

    pdbs_by_namespace = {}
    for pdb in pdb_items:
        pdbs_by_namespace.setdefault(pdb["namespace"], []).append(pdb)

    workload_items = []
    for item in deployments.get("items", []):
        workload_items.append(
            extract_workload_summary(item, pdbs_by_namespace, exempt_namespaces, exempt_workloads)
        )
    for item in statefulsets.get("items", []):
        workload_items.append(
            extract_workload_summary(item, pdbs_by_namespace, exempt_namespaces, exempt_workloads)
        )

    single_replica = [
        workload
        for workload in workload_items
        if workload["single_replica"]
        and not workload["exempt_namespace"]
        and not workload["exempt_workload"]
    ]
    pdb_unprotected = [
        workload
        for workload in workload_items
        if (
            not workload["single_replica"]
            and not workload["pdb_protected"]
            and not workload["exempt_namespace"]
            and not workload["exempt_workload"]
        )
    ]

    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "server_version": version_payload.get("serverVersion", {}).get("gitVersion"),
        "nodes": node_items,
        "workloads": workload_items,
        "pod_disruption_budgets": pdb_items,
        "single_replica_workloads": single_replica,
        "pdb_unprotected_workloads": pdb_unprotected,
        "summary": {
            "node_count": len(node_items),
            "ready_node_count": len([node for node in node_items if node["ready"]]),
            "control_plane_count": len([node for node in node_items if node["roles"]]),
            "single_replica_workload_count": len(single_replica),
            "pdb_unprotected_workload_count": len(pdb_unprotected),
        },
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Capture Kubernetes cluster upgrade risk and state report."
    )
    parser.add_argument("--kubeconfig", required=True, help="Path to kubeconfig.")
    parser.add_argument("--output", required=True, help="Output JSON file path.")
    parser.add_argument(
        "--exempt-namespace",
        action="append",
        default=[],
        help="Namespace that should be excluded from workload safety assertions.",
    )
    parser.add_argument(
        "--exempt-workload",
        action="append",
        default=[],
        help=(
            "Workload identifier in namespace/name format that should be "
            "excluded from workload safety assertions."
        ),
    )
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        report = build_report(
            args.kubeconfig,
            set(args.exempt_namespace),
            set(args.exempt_workload),
        )
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(exc.stderr)
        return 1

    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
