#!/usr/bin/env python3

import argparse
import http.client
import json
import math
import socket
import statistics
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path


def run(command, *, timeout=60, input_text=None, check=True):
    return subprocess.run(
        command,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=input_text,
    )


def kubectl(kubeconfig, arguments, *, timeout=60, input_text=None, check=True):
    return run(
        ["kubectl", f"--kubeconfig={kubeconfig}", *arguments],
        timeout=timeout,
        input_text=input_text,
        check=check,
    )


def kubectl_json(kubeconfig, arguments, *, timeout=60):
    return json.loads(kubectl(kubeconfig, arguments, timeout=timeout).stdout)


def percentile(values, wanted):
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil((wanted / 100) * len(ordered)))
    return ordered[rank - 1]


def summarize(values, digits=2):
    if not values:
        return {}
    return {
        "samples": len(values),
        "minimum": round(min(values), digits),
        "median": round(statistics.median(values), digits),
        "p95": round(percentile(values, 95), digits),
        "maximum": round(max(values), digits),
    }


def available_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_for_proxy(port, process):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read()
            raise RuntimeError(f"kubectl proxy exited before becoming ready: {stderr}")
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            connection.request("GET", "/readyz")
            response = connection.getresponse()
            payload = response.read()
            connection.close()
            if response.status == 200 and payload.strip() == b"ok":
                return
        except OSError:
            pass
        time.sleep(0.2)
    raise TimeoutError("kubectl proxy did not become ready within 15 seconds")


def benchmark_api(kubeconfig, sample_count):
    port = available_port()
    process = subprocess.Popen(
        [
            "kubectl",
            f"--kubeconfig={kubeconfig}",
            "proxy",
            f"--port={port}",
            "--accept-hosts=^127[.]0[.]0[.]1$",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_for_proxy(port, process)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        samples = []
        for index in range(sample_count + 5):
            started = time.perf_counter()
            connection.request("GET", "/readyz")
            response = connection.getresponse()
            body = response.read()
            elapsed_ms = (time.perf_counter() - started) * 1000
            if response.status != 200 or body.strip() != b"ok":
                raise RuntimeError(f"API readiness returned HTTP {response.status}: {body!r}")
            if index >= 5:
                samples.append(elapsed_ms)
        connection.close()
        return summarize(samples)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def pod_definition(namespace, name, node, command):
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app.kubernetes.io/name": "cluster-performance-benchmark"},
        },
        "spec": {
            "automountServiceAccountToken": False,
            "restartPolicy": "Never",
            "terminationGracePeriodSeconds": 0,
            "nodeSelector": {"kubernetes.io/hostname": node},
            "tolerations": [{"operator": "Exists", "effect": "NoSchedule"}],
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 65534,
                "runAsGroup": 65534,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "benchmark",
                    "image": "",
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["sh", "-c", command],
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "16Mi"},
                        "limits": {"cpu": "1", "memory": "64Mi"},
                    },
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                }
            ],
        },
    }


def apply_pod(kubeconfig, definition, image):
    definition["spec"]["containers"][0]["image"] = image
    kubectl(
        kubeconfig,
        ["apply", "--filename=-"],
        input_text=json.dumps(definition),
    )


def wait_for_pod(kubeconfig, namespace, name, *, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pod = kubectl_json(
            kubeconfig,
            ["get", "pod", name, "-n", namespace, "--output=json"],
        )
        conditions = pod.get("status", {}).get("conditions", [])
        if any(item.get("type") == "Ready" and item.get("status") == "True" for item in conditions):
            return pod
        if pod.get("status", {}).get("phase") == "Failed":
            raise RuntimeError(f"Pod {namespace}/{name} failed: {pod['status']}")
        time.sleep(0.25)
    raise TimeoutError(f"Pod {namespace}/{name} was not Ready within {timeout}s")


def benchmark_pod_startup(kubeconfig, namespace, nodes, image, sample_count):
    samples = []
    for index in range(sample_count):
        name = f"startup-{index}"
        node = nodes[index % len(nodes)]
        definition = pod_definition(namespace, name, node, "sleep 300")
        started = time.perf_counter()
        apply_pod(kubeconfig, definition, image)
        wait_for_pod(kubeconfig, namespace, name)
        samples.append((time.perf_counter() - started) * 1000)
        kubectl(
            kubeconfig,
            [
                "delete",
                "pod",
                name,
                "-n",
                namespace,
                "--wait=true",
                "--timeout=30s",
            ],
        )
    return summarize(samples)


def create_utility_pods(kubeconfig, namespace, nodes, image):
    pods = {}
    for index, node in enumerate(nodes):
        name = f"utility-{index}"
        apply_pod(
            kubeconfig,
            pod_definition(namespace, name, node, "sleep 1800"),
            image,
        )
        pods[node] = name
    for name in pods.values():
        wait_for_pod(kubeconfig, namespace, name)
    return pods


def exec_shell(kubeconfig, namespace, pod, script, *, timeout=90):
    result = kubectl(
        kubeconfig,
        ["exec", "-n", namespace, pod, "--", "sh", "-c", script],
        timeout=timeout,
    )
    return result.stdout.strip()


def benchmark_dns(kubeconfig, namespace, utility_pods, query_count):
    results = []
    for node, pod in utility_pods.items():
        script = (
            "start=$(cut -d' ' -f1 /proc/uptime); "
            "i=0; "
            f'while [ "$i" -lt {query_count} ]; do '
            "nslookup kubernetes.default.svc.cluster.local >/dev/null || exit 1; "
            "i=$((i + 1)); "
            "done; "
            "end=$(cut -d' ' -f1 /proc/uptime); "
            f'awk -v start="$start" -v end="$end" '
            f"'BEGIN {{ printf \"%.3f\", ((end-start)*1000)/{query_count} }}'"
        )
        results.append(
            {
                "node": node,
                "queries": query_count,
                "average_ms": float(exec_shell(kubeconfig, namespace, pod, script, timeout=60)),
            }
        )
    return results


def benchmark_network(
    kubeconfig,
    namespace,
    nodes,
    utility_pods,
    image,
    transfer_mib,
):
    server_node = next(
        (
            node
            for node in nodes
            if not any(
                label.startswith("node-role.kubernetes.io/control-plane")
                for label in node["labels"]
            )
        ),
        nodes[0],
    )
    server_name = "network-server"
    server = pod_definition(
        namespace,
        server_name,
        server_node["name"],
        "while true; do nc -l -p 9000 >/dev/null; done",
    )
    apply_pod(kubeconfig, server, image)
    server_pod = wait_for_pod(kubeconfig, namespace, server_name)
    server_ip = server_pod["status"]["podIP"]
    results = []
    for node, pod in utility_pods.items():
        script = (
            "start=$(cut -d' ' -f1 /proc/uptime); "
            f"dd if=/dev/zero bs=1M count={transfer_mib} 2>/dev/null "
            f"| nc -w 30 {server_ip} 9000; "
            "end=$(cut -d' ' -f1 /proc/uptime); "
            'awk -v start="$start" -v end="$end" '
            f"-v mib={transfer_mib} "
            "'BEGIN { duration=end-start; "
            'if (duration <= 0) duration=0.01; printf "%.2f", mib/duration }\''
        )
        results.append(
            {
                "source_node": node,
                "destination_node": server_node["name"],
                "transfer_mib": transfer_mib,
                "mib_per_second": float(exec_shell(kubeconfig, namespace, pod, script, timeout=60)),
            }
        )
    return results


def ready_nodes(kubeconfig):
    payload = kubectl_json(kubeconfig, ["get", "nodes", "--output=json"])
    nodes = []
    for item in payload.get("items", []):
        ready = any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in item.get("status", {}).get("conditions", [])
        )
        if not ready:
            raise RuntimeError(f"Node {item['metadata']['name']} is not Ready")
        labels = item.get("metadata", {}).get("labels", {})
        nodes.append(
            {
                "name": item["metadata"]["name"],
                "labels": list(labels),
            }
        )
    if not nodes:
        raise RuntimeError("No Ready Kubernetes nodes were found")
    return nodes


def render_report(result):
    api = result["results"]["api_ready_latency_ms"]
    startup = result["results"]["pod_startup_latency_ms"]
    dns = result["results"]["dns"]
    network = result["results"]["network"]
    lines = [
        "# Active-safe cluster benchmark",
        "",
        f"- Run: `{result['run_id']}`",
        f"- Experiment: `{result['experiment']}`",
        f"- Kubernetes nodes: {len(result['nodes'])}",
        "",
        "## Results",
        "",
        "| Signal | Median / average | p95 |",
        "| --- | ---: | ---: |",
        f"| Persistent API `/readyz` | {api['median']:.2f} ms | {api['p95']:.2f} ms |",
        f"| Pod startup to Ready | {startup['median']:.2f} ms | {startup['p95']:.2f} ms |",
        "",
        "| DNS source node | Average query latency |",
        "| --- | ---: |",
    ]
    for item in dns:
        lines.append(f"| {item['node']} | {item['average_ms']:.3f} ms |")
    lines.extend(
        [
            "",
            "| Network path | Throughput |",
            "| --- | ---: |",
        ]
    )
    for item in network:
        lines.append(
            f"| {item['source_node']} -> {item['destination_node']} "
            f"| {item['mib_per_second']:.2f} MiB/s |"
        )
    comparison = result.get("comparison")
    if comparison:
        lines.extend(
            [
                "",
                f"## Comparison with control `{comparison['control_run_id']}`",
                "",
                "| Metric | Delta | Direction |",
                "| --- | ---: | --- |",
                (
                    "| API median | "
                    f"{comparison['api_median_delta_percent']:+.2f}% | lower is better |"
                ),
                (
                    "| Pod startup median | "
                    f"{comparison['pod_startup_median_delta_percent']:+.2f}% "
                    "| lower is better |"
                ),
                (f"| DNS mean | {comparison['dns_mean_delta_percent']:+.2f}% | lower is better |"),
                (
                    "| Cross-node network mean | "
                    f"{comparison['network_mean_delta_percent']:+.2f}% "
                    "| higher is better |"
                ),
            ]
        )
    lines.extend(
        [
            "",
            "## Safety envelope",
            "",
            "- The benchmark used an isolated, ephemeral namespace.",
            "- Traffic and sample counts were bounded; no storage workload ran.",
            "- All benchmark resources were requested for deletion after capture.",
            "",
        ]
    )
    return "\n".join(lines)


def metric_means(result):
    dns = [item["average_ms"] for item in result["results"]["dns"]]
    network = [
        item["mib_per_second"]
        for item in result["results"]["network"]
        if item["source_node"] != item["destination_node"]
    ]
    return {
        "api_median": result["results"]["api_ready_latency_ms"]["median"],
        "pod_startup_median": result["results"]["pod_startup_latency_ms"]["median"],
        "dns_mean": statistics.mean(dns),
        "network_mean": statistics.mean(network),
    }


def delta_percent(current, control):
    if control == 0:
        return 0.0
    return round(((current - control) / control) * 100, 2)


def latest_control(output_root):
    for path in sorted(output_root.glob("*/results.json"), reverse=True):
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if candidate.get("experiment") == "control":
            return candidate
    return None


def compare_with_control(result, control):
    current_metrics = metric_means(result)
    control_metrics = metric_means(control)
    return {
        "control_run_id": control["run_id"],
        "api_median_delta_percent": delta_percent(
            current_metrics["api_median"],
            control_metrics["api_median"],
        ),
        "pod_startup_median_delta_percent": delta_percent(
            current_metrics["pod_startup_median"],
            control_metrics["pod_startup_median"],
        ),
        "dns_mean_delta_percent": delta_percent(
            current_metrics["dns_mean"],
            control_metrics["dns_mean"],
        ),
        "network_mean_delta_percent": delta_percent(
            current_metrics["network_mean"],
            control_metrics["network_mean"],
        ),
    }


def execute(args):
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    namespace = f"cluster-benchmark-{run_id.lower()}"
    nodes = ready_nodes(args.kubeconfig)
    namespace_definition = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": namespace,
            "labels": {
                "pod-security.kubernetes.io/enforce": "restricted",
                "pod-security.kubernetes.io/enforce-version": "latest",
            },
        },
    }
    try:
        kubectl(
            args.kubeconfig,
            ["apply", "--filename=-"],
            input_text=json.dumps(namespace_definition),
        )
        api = benchmark_api(args.kubeconfig, args.api_samples)
        startup = benchmark_pod_startup(
            args.kubeconfig,
            namespace,
            [node["name"] for node in nodes],
            args.image,
            args.pod_startup_samples,
        )
        utility_pods = create_utility_pods(
            args.kubeconfig,
            namespace,
            [node["name"] for node in nodes],
            args.image,
        )
        dns = benchmark_dns(
            args.kubeconfig,
            namespace,
            utility_pods,
            args.dns_queries,
        )
        network = benchmark_network(
            args.kubeconfig,
            namespace,
            nodes,
            utility_pods,
            args.image,
            args.network_transfer_mib,
        )
        result = {
            "schema_version": 1,
            "profile": "active-safe",
            "run_id": run_id,
            "captured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "experiment": args.experiment,
            "nodes": [node["name"] for node in nodes],
            "limits": {
                "api_samples": args.api_samples,
                "pod_startup_samples": args.pod_startup_samples,
                "dns_queries_per_node": args.dns_queries,
                "network_transfer_mib_per_node": args.network_transfer_mib,
            },
            "results": {
                "api_ready_latency_ms": api,
                "pod_startup_latency_ms": startup,
                "dns": dns,
                "network": network,
            },
        }
        control = latest_control(args.output_root)
        if args.experiment != "control" and control is not None:
            result["comparison"] = compare_with_control(result, control)
        run_dir = args.output_root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / "results.json").write_text(
            json.dumps(result, indent=2) + "\n",
            encoding="utf-8",
        )
        report = run_dir / "report.md"
        report.write_text(render_report(result), encoding="utf-8")
        print(f"Active-safe benchmark report: {report}")
        return result
    finally:
        cleanup = kubectl(
            args.kubeconfig,
            [
                "delete",
                "namespace",
                namespace,
                "--wait=true",
                "--timeout=120s",
                "--ignore-not-found=true",
            ],
            timeout=130,
            check=False,
        )
        if cleanup.returncode != 0:
            raise RuntimeError(f"Benchmark namespace cleanup failed: {cleanup.stderr.strip()}")


def bounded_integer(minimum, maximum):
    def parse(value):
        parsed = int(value)
        if parsed < minimum or parsed > maximum:
            raise argparse.ArgumentTypeError(f"must be between {minimum} and {maximum}")
        return parsed

    return parse


def main():
    parser = argparse.ArgumentParser(
        description="Run a bounded, ephemeral Kubernetes performance benchmark."
    )
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--experiment", default="control")
    parser.add_argument(
        "--api-samples",
        type=bounded_integer(10, 200),
        default=50,
    )
    parser.add_argument(
        "--pod-startup-samples",
        type=bounded_integer(4, 20),
        default=8,
    )
    parser.add_argument(
        "--dns-queries",
        type=bounded_integer(5, 100),
        default=20,
    )
    parser.add_argument(
        "--network-transfer-mib",
        type=bounded_integer(1, 64),
        default=8,
    )
    args = parser.parse_args()
    execute(args)


if __name__ == "__main__":
    main()
