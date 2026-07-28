#!/usr/bin/env python3

import argparse
import json
import math
import statistics
import subprocess
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path


def run(command, timeout=30):
    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def kubectl(kubeconfig, arguments, timeout=30):
    result = run(["kubectl", f"--kubeconfig={kubeconfig}", *arguments], timeout=timeout)
    return result.stdout


def kubectl_json(kubeconfig, arguments, timeout=30):
    return json.loads(kubectl(kubeconfig, arguments, timeout=timeout))


def percentile(values, percentile_value):
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil((percentile_value / 100) * len(ordered)))
    return ordered[rank - 1]


def latency_summary(samples):
    successful = [sample["latency_ms"] for sample in samples if sample.get("ok")]
    if not successful:
        return {
            "samples": len(samples),
            "successful_samples": 0,
            "minimum_ms": None,
            "median_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "maximum_ms": None,
        }
    return {
        "samples": len(samples),
        "successful_samples": len(successful),
        "minimum_ms": round(min(successful), 2),
        "median_ms": round(statistics.median(successful), 2),
        "p95_ms": round(percentile(successful, 95), 2),
        "p99_ms": round(percentile(successful, 99), 2),
        "maximum_ms": round(max(successful), 2),
    }


def sample_api_latency(kubeconfig, sample_count):
    samples = []
    for _ in range(sample_count):
        started = time.perf_counter()
        try:
            response = kubectl(kubeconfig, ["get", "--raw=/readyz"], timeout=10)
            samples.append(
                {
                    "ok": response.strip() == "ok",
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                    "response": response.strip(),
                }
            )
        except (subprocess.SubprocessError, OSError) as error:
            samples.append(
                {
                    "ok": False,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                    "error": str(error),
                }
            )
    return samples


def summarize_nodes(nodes):
    summaries = []
    for item in nodes.get("items", []):
        conditions = {
            condition["type"]: condition["status"]
            for condition in item.get("status", {}).get("conditions", [])
        }
        node_info = item.get("status", {}).get("nodeInfo", {})
        summaries.append(
            {
                "name": item.get("metadata", {}).get("name", ""),
                "ready": conditions.get("Ready") == "True",
                "conditions": conditions,
                "capacity": item.get("status", {}).get("capacity", {}),
                "allocatable": item.get("status", {}).get("allocatable", {}),
                "architecture": node_info.get("architecture", ""),
                "container_runtime_version": node_info.get("containerRuntimeVersion", ""),
                "kernel_version": node_info.get("kernelVersion", ""),
                "kubelet_version": node_info.get("kubeletVersion", ""),
                "os_image": node_info.get("osImage", ""),
            }
        )
    return summaries


def summarize_pods(pods):
    summaries = []
    for item in pods.get("items", []):
        statuses = item.get("status", {}).get("containerStatuses", []) or []
        owners = item.get("metadata", {}).get("ownerReferences", []) or []
        summaries.append(
            {
                "namespace": item.get("metadata", {}).get("namespace", ""),
                "name": item.get("metadata", {}).get("name", ""),
                "node": item.get("spec", {}).get("nodeName", ""),
                "phase": item.get("status", {}).get("phase", "Unknown"),
                "restarts": sum(status.get("restartCount", 0) for status in statuses),
                "owner_kind": owners[0].get("kind", "") if owners else "",
                "owner_name": owners[0].get("name", "") if owners else "",
            }
        )
    return summaries


def capture_cluster(
    kubeconfig,
    output,
    api_samples,
    repository_revision,
    expected_kubernetes_version,
):
    version = kubectl_json(kubeconfig, ["version", "--output=json"])
    nodes_raw = kubectl_json(kubeconfig, ["get", "nodes", "--output=json"])
    pods_raw = kubectl_json(
        kubeconfig,
        ["get", "pods", "--all-namespaces", "--output=json"],
    )
    workloads = kubectl_json(
        kubeconfig,
        [
            "get",
            "deployments,statefulsets,daemonsets",
            "--all-namespaces",
            "--output=json",
        ],
    )
    storage = kubectl_json(
        kubeconfig,
        ["get", "persistentvolumes,persistentvolumeclaims", "--all-namespaces", "--output=json"],
    )
    node_summaries = summarize_nodes(nodes_raw)
    stats = {}
    stats_errors = {}
    for node in node_summaries:
        name = node["name"]
        try:
            stats[name] = kubectl_json(
                kubeconfig,
                ["get", f"--raw=/api/v1/nodes/{name}/proxy/stats/summary"],
                timeout=20,
            )
        except (subprocess.SubprocessError, OSError, json.JSONDecodeError) as error:
            stats_errors[name] = str(error)

    latency_samples = sample_api_latency(kubeconfig, api_samples)
    payload = {
        "schema_version": 1,
        "profile": "observe",
        "captured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repository_revision": repository_revision,
        "expected_kubernetes_version": expected_kubernetes_version,
        "version": version,
        "nodes": node_summaries,
        "pods": summarize_pods(pods_raw),
        "workloads": workloads,
        "storage": storage,
        "node_stats_summary": stats,
        "node_stats_errors": stats_errors,
        "api_readyz_latency": {
            "samples": latency_samples,
            "summary": latency_summary(latency_samples),
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def percent(part, total):
    if not total:
        return None
    return round((part / total) * 100, 2)


def root_filesystem(node):
    for filesystem in node.get("storage", {}).get("filesystems", []):
        if filesystem.get("mountpoint") == "/":
            return filesystem
    return {}


def maximum_temperature(node):
    temperatures = node.get("cpu", {}).get("temperatures", [])
    values = [item.get("celsius") for item in temperatures if item.get("celsius") is not None]
    return max(values) if values else None


def pressure_avg10(node, resource, category):
    return node.get("pressure", {}).get(resource, {}).get(category, {}).get("avg10")


def throttling_value(node):
    output = node.get("cpu", {}).get("raspberry_pi_throttling", {}).get("stdout", "")
    if "=" not in output:
        return None
    try:
        return int(output.split("=", 1)[1], 16)
    except ValueError:
        return None


def node_summary(name, node):
    cpu_count = node.get("cpu", {}).get("logical_cpu_count") or 0
    load_one = node.get("cpu", {}).get("load_average", {}).get("one_minute")
    memory = node.get("memory", {})
    filesystem = root_filesystem(node)
    return {
        "name": name,
        "architecture": node.get("node", {}).get("architecture", ""),
        "kernel": node.get("node", {}).get("kernel", ""),
        "uptime_seconds": node.get("node", {}).get("uptime_seconds", 0),
        "logical_cpu_count": cpu_count,
        "load_one": load_one,
        "load_one_per_cpu": (
            round(load_one / cpu_count, 3) if load_one is not None and cpu_count else None
        ),
        "memory_available_percent": percent(
            memory.get("available_bytes", 0),
            memory.get("total_bytes", 0),
        ),
        "swap_total_bytes": memory.get("swap_total_bytes", 0),
        "maximum_temperature_celsius": maximum_temperature(node),
        "throttling_value": throttling_value(node),
        "cpu_pressure_some_avg10": pressure_avg10(node, "cpu", "some"),
        "memory_pressure_full_avg10": pressure_avg10(node, "memory", "full"),
        "io_pressure_full_avg10": pressure_avg10(node, "io", "full"),
        "root_used_percent": filesystem.get("used_percent"),
        "root_available_bytes": filesystem.get("available_bytes"),
        "service_states": node.get("services", {}).get("states", {}),
        "failed_units": node.get("services", {}).get("failed_units", []),
        "path_sizes": node.get("storage", {}).get("path_sizes", {}),
    }


def failed_unit_names(failed_units):
    names = []
    for line in failed_units:
        fields = line.split()
        if fields:
            names.append(fields[0])
    return names


def add_finding(findings, severity, scope, message):
    findings.append({"severity": severity, "scope": scope, "message": message})


def evaluate_findings(cluster, nodes):
    findings = []
    expected_version = cluster.get("expected_kubernetes_version", "").lstrip("v")
    actual_version = (
        cluster.get("version", {}).get("serverVersion", {}).get("gitVersion", "").lstrip("v")
    )
    if expected_version and actual_version != expected_version:
        add_finding(
            findings,
            "warn",
            "cluster",
            f"Kubernetes is {actual_version}; the catalog expects {expected_version}.",
        )

    unready = [node["name"] for node in cluster["nodes"] if not node["ready"]]
    if unready:
        add_finding(findings, "fail", "cluster", f"Nodes not Ready: {', '.join(unready)}")

    phases = Counter(pod["phase"] for pod in cluster["pods"])
    if phases.get("Failed", 0):
        add_finding(
            findings,
            "warn",
            "cluster",
            f"{phases['Failed']} Pod(s) are in Failed phase.",
        )
    if phases.get("Pending", 0):
        add_finding(
            findings,
            "warn",
            "cluster",
            f"{phases['Pending']} Pod(s) are Pending.",
        )

    api_p95 = cluster["api_readyz_latency"]["summary"].get("p95_ms")
    if api_p95 is None:
        add_finding(findings, "fail", "api", "No API readiness latency sample succeeded.")
    elif api_p95 > 1000:
        add_finding(findings, "fail", "api", f"API readyz p95 is {api_p95:.2f} ms.")
    elif api_p95 > 500:
        add_finding(findings, "warn", "api", f"API readyz p95 is {api_p95:.2f} ms.")

    for node in nodes:
        scope = node["name"]
        inactive = [
            f"{name}={state}" for name, state in node["service_states"].items() if state != "active"
        ]
        if inactive:
            add_finding(findings, "fail", scope, f"Inactive services: {', '.join(inactive)}")
        if node["failed_units"]:
            units = failed_unit_names(node["failed_units"])
            add_finding(
                findings,
                "warn",
                scope,
                f"Failed systemd units: {', '.join(units)}.",
            )
        temperature = node["maximum_temperature_celsius"]
        if temperature is not None and temperature >= 80:
            add_finding(findings, "fail", scope, f"Temperature reached {temperature:.1f} C.")
        elif temperature is not None and temperature >= 70:
            add_finding(findings, "warn", scope, f"Temperature reached {temperature:.1f} C.")
        if node["throttling_value"]:
            add_finding(
                findings,
                "warn",
                scope,
                f"Raspberry Pi throttling flags are 0x{node['throttling_value']:x}.",
            )
        memory_available = node["memory_available_percent"]
        if memory_available is not None and memory_available < 8:
            add_finding(findings, "fail", scope, f"Available memory is {memory_available:.1f}%.")
        elif memory_available is not None and memory_available < 15:
            add_finding(findings, "warn", scope, f"Available memory is {memory_available:.1f}%.")
        root_used = node["root_used_percent"]
        if root_used is not None and root_used > 92:
            add_finding(findings, "fail", scope, f"Root filesystem is {root_used:.1f}% used.")
        elif root_used is not None and root_used > 85:
            add_finding(findings, "warn", scope, f"Root filesystem is {root_used:.1f}% used.")
        load_per_cpu = node["load_one_per_cpu"]
        if load_per_cpu is not None and load_per_cpu > 1:
            add_finding(
                findings,
                "warn",
                scope,
                f"One-minute load per CPU is {load_per_cpu:.2f}.",
            )
        io_pressure = node["io_pressure_full_avg10"]
        if io_pressure is not None and io_pressure > 2:
            add_finding(
                findings,
                "warn",
                scope,
                f"Full I/O PSI avg10 is {io_pressure:.2f}%.",
            )
    return findings


def previous_summary(run_dir):
    candidates = sorted(
        path / "summary.json"
        for path in run_dir.parent.iterdir()
        if path.is_dir() and path != run_dir and (path / "summary.json").is_file()
    )
    if not candidates:
        return None
    return json.loads(candidates[-1].read_text(encoding="utf-8"))


def delta_percent(current, previous):
    if current is None or previous in (None, 0):
        return None
    return round(((current - previous) / previous) * 100, 2)


def build_comparison(current, previous):
    if not previous:
        return None
    comparison = {
        "previous_run_id": previous.get("run_id", ""),
        "api_p95_delta_percent": delta_percent(
            current["cluster"]["api_readyz_latency"]["summary"].get("p95_ms"),
            previous.get("cluster", {})
            .get("api_readyz_latency", {})
            .get("summary", {})
            .get("p95_ms"),
        ),
        "nodes": [],
    }
    previous_nodes = {node["name"]: node for node in previous.get("nodes", [])}
    for node in current["nodes"]:
        old = previous_nodes.get(node["name"], {})
        comparison["nodes"].append(
            {
                "name": node["name"],
                "temperature_delta_celsius": (
                    round(
                        node["maximum_temperature_celsius"] - old["maximum_temperature_celsius"],
                        2,
                    )
                    if node["maximum_temperature_celsius"] is not None
                    and old.get("maximum_temperature_celsius") is not None
                    else None
                ),
                "load_per_cpu_delta_percent": delta_percent(
                    node["load_one_per_cpu"],
                    old.get("load_one_per_cpu"),
                ),
                "memory_available_delta_points": (
                    round(
                        node["memory_available_percent"] - old["memory_available_percent"],
                        2,
                    )
                    if node["memory_available_percent"] is not None
                    and old.get("memory_available_percent") is not None
                    else None
                ),
            }
        )
    return comparison


def format_value(value, suffix="", decimals=2):
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{decimals}f}{suffix}"
    return f"{value}{suffix}"


def format_bytes(value):
    if value is None:
        return "n/a"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    converted = float(value)
    for unit in units:
        if abs(converted) < 1024 or unit == units[-1]:
            return f"{converted:.1f} {unit}"
        converted /= 1024
    return "n/a"


def render_markdown(summary):
    cluster = summary["cluster"]
    api = cluster["api_readyz_latency"]["summary"]
    phases = Counter(pod["phase"] for pod in cluster["pods"])
    lines = [
        f"# Cluster performance baseline: {summary['run_id']}",
        "",
        f"- Captured: `{summary['captured_at']}`",
        f"- Profile: `{summary['profile']}`",
        f"- Repository revision: `{summary['repository_revision']}`",
        f"- Kubernetes: `{cluster['version']['serverVersion']['gitVersion']}`",
        (f"- Catalog target: `{cluster.get('expected_kubernetes_version') or 'not set'}`"),
        (
            f"- Nodes Ready: `{sum(node['ready'] for node in cluster['nodes'])}"
            f"/{len(cluster['nodes'])}`"
        ),
        f"- Pods: `{len(cluster['pods'])}` "
        f"(Running={phases.get('Running', 0)}, Pending={phases.get('Pending', 0)}, "
        f"Failed={phases.get('Failed', 0)})",
        "",
        "## API latency",
        "",
        "| Samples | Median | p95 | p99 | Max |",
        "| ---: | ---: | ---: | ---: | ---: |",
        (
            f"| {api['successful_samples']}/{api['samples']} "
            f"| {format_value(api['median_ms'], ' ms')} "
            f"| {format_value(api['p95_ms'], ' ms')} "
            f"| {format_value(api['p99_ms'], ' ms')} "
            f"| {format_value(api['maximum_ms'], ' ms')} |"
        ),
        "",
        "## Cumulative Pod restarts",
        "",
        f"- Total reported restart count: `{sum(pod['restarts'] for pod in cluster['pods'])}`",
        "",
        "| Namespace | Pod | Node | Restarts |",
        "| --- | --- | --- | ---: |",
    ]
    restarted_pods = sorted(
        (pod for pod in cluster["pods"] if pod["restarts"] > 0),
        key=lambda pod: pod["restarts"],
        reverse=True,
    )
    if restarted_pods:
        for pod in restarted_pods[:10]:
            lines.append(
                f"| {pod['namespace']} | {pod['name']} | {pod['node']} | {pod['restarts']} |"
            )
    else:
        lines.append("| - | None | - | 0 |")

    lines.extend(
        [
            "",
            "## Nodes",
            "",
            (
                "| Node | Temp | Load/CPU | Memory available | Root used "
                "| I/O PSI full avg10 | Throttling |"
            ),
            "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for node in summary["nodes"]:
        throttling = (
            f"0x{node['throttling_value']:x}"
            if node["throttling_value"] is not None
            else "unavailable"
        )
        lines.append(
            f"| {node['name']} "
            f"| {format_value(node['maximum_temperature_celsius'], ' C', 1)} "
            f"| {format_value(node['load_one_per_cpu'], decimals=3)} "
            f"| {format_value(node['memory_available_percent'], '%', 1)} "
            f"| {format_value(node['root_used_percent'], '%', 1)} "
            f"| {format_value(node['io_pressure_full_avg10'], '%')} "
            f"| {throttling} |"
        )

    lines.extend(
        [
            "",
            "## Runtime storage",
            "",
            "| Node | containerd | kubelet | etcd | Logs | APT cache |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    storage_paths = (
        "/var/lib/containerd",
        "/var/lib/kubelet",
        "/var/lib/etcd",
        "/var/log",
        "/var/cache/apt",
    )
    for node in summary["nodes"]:
        values = []
        for path in storage_paths:
            path_size = node["path_sizes"].get(path, {})
            values.append(format_bytes(path_size.get("bytes")))
        lines.append(f"| {node['name']} | {' | '.join(values)} |")

    lines.extend(["", "## Findings", ""])
    if summary["findings"]:
        for finding in summary["findings"]:
            lines.append(
                f"- **{finding['severity'].upper()}** `{finding['scope']}`: {finding['message']}"
            )
    else:
        lines.append("- No threshold findings in this observation.")

    comparison = summary.get("comparison")
    if comparison:
        lines.extend(
            [
                "",
                f"## Comparison with {comparison['previous_run_id']}",
                "",
                "| Metric | Delta |",
                "| --- | ---: |",
                (f"| API p95 | {format_value(comparison['api_p95_delta_percent'], '%')} |"),
            ]
        )
        for node in comparison["nodes"]:
            lines.append(
                f"| {node['name']} temperature "
                f"| {format_value(node['temperature_delta_celsius'], ' C')} |"
            )
            lines.append(
                f"| {node['name']} load/CPU "
                f"| {format_value(node['load_per_cpu_delta_percent'], '%')} |"
            )
            lines.append(
                f"| {node['name']} memory available "
                f"| {format_value(node['memory_available_delta_points'], ' points')} |"
            )

    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "This is a read-only point-in-time observation. Repeat it under comparable "
            "idle conditions before treating a delta as a regression. Synthetic CPU, "
            "network and storage load is intentionally outside the `observe` profile.",
            "",
        ]
    )
    return "\n".join(lines)


def build_report(run_dir):
    cluster = json.loads((run_dir / "cluster.json").read_text(encoding="utf-8"))
    nodes = []
    for path in sorted((run_dir / "nodes").glob("*.json")):
        nodes.append(node_summary(path.stem, json.loads(path.read_text(encoding="utf-8"))))
    if not nodes:
        raise RuntimeError(f"No node captures found below {run_dir / 'nodes'}")
    summary = {
        "schema_version": 1,
        "run_id": run_dir.name,
        "profile": cluster["profile"],
        "captured_at": cluster["captured_at"],
        "repository_revision": cluster.get("repository_revision", "unknown"),
        "cluster": cluster,
        "nodes": nodes,
    }
    summary["findings"] = evaluate_findings(cluster, nodes)
    summary["comparison"] = build_comparison(summary, previous_summary(run_dir))
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )
    report = run_dir / "report.md"
    report.write_text(render_markdown(summary), encoding="utf-8")
    return summary, report


def main():
    parser = argparse.ArgumentParser(description="Capture and report cluster baselines.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser("capture-cluster")
    capture.add_argument("--kubeconfig", required=True)
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--api-samples", type=int, default=15)
    capture.add_argument("--repository-revision", default="unknown")
    capture.add_argument("--expected-kubernetes-version", default="")

    report = subparsers.add_parser("report")
    report.add_argument("--run-dir", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "capture-cluster":
        if args.api_samples < 3 or args.api_samples > 100:
            parser.error("--api-samples must be between 3 and 100")
        payload = capture_cluster(
            args.kubeconfig,
            args.output,
            args.api_samples,
            args.repository_revision,
            args.expected_kubernetes_version,
        )
        print(
            f"Captured {len(payload['nodes'])} nodes and "
            f"{len(payload['pods'])} Pods into {args.output}"
        )
        return

    summary, report_path = build_report(args.run_dir)
    severities = Counter(finding["severity"] for finding in summary["findings"])
    print(f"Baseline report: {report_path}")
    print(f"Findings: fail={severities.get('fail', 0)}, warn={severities.get('warn', 0)}")


if __name__ == "__main__":
    main()
