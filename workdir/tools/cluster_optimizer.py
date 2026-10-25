#!/usr/bin/env python3

import argparse
import json
import statistics
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

WORKDIR = Path(__file__).resolve().parents[1]
INVENTORY = WORKDIR / "inventory" / "hosts.ini"
OUTPUTS = Path("/home/ansible/.kube/outputs")
KUBECONFIG = Path("/home/ansible/.kube/config")


def run(command):
    print(f"+ {' '.join(str(item) for item in command)}", flush=True)
    return subprocess.run(command, cwd=WORKDIR, check=True)


def playbook(name, extra_vars=None):
    command = ["ansible-playbook", "-i", str(INVENTORY), str(WORKDIR / "playbooks" / name)]
    if extra_vars:
        command.extend(["--extra-vars", json.dumps(extra_vars, separators=(",", ":"))])
    run(command)


class RunIds:
    def __init__(self):
        self.previous = ""

    def next(self):
        while True:
            value = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            if value != self.previous:
                self.previous = value
                return value
            time.sleep(0.1)


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def capture_baseline(run_ids):
    run_id = run_ids.next()
    playbook(
        "25-capture-performance-baseline.yml",
        {
            "performance_baseline_run_id": run_id,
            "performance_baseline_repository_revision": "optimization-flow",
            "performance_baseline_include_path_sizes": False,
        },
    )
    return load_json(OUTPUTS / "benchmarks" / run_id / "summary.json")


def capture_benchmark(run_ids, experiment):
    run_id = run_ids.next()
    playbook(
        "27-run-performance-benchmark.yml",
        {
            "performance_benchmark_run_id": run_id,
            "performance_benchmark_experiment": experiment,
        },
    )
    return load_json(OUTPUTS / "benchmarks-active" / run_id / "results.json")


def metric_values(result):
    payload = result["results"]
    cross_node = [
        item["mib_per_second"]
        for item in payload["network"]
        if item["source_node"] != item["destination_node"]
    ]
    return {
        "api": payload["api_ready_latency_ms"]["median"],
        "startup": payload["pod_startup_latency_ms"]["median"],
        "dns": statistics.mean(item["average_ms"] for item in payload["dns"]),
        "cpu": statistics.median(item["milliseconds"] for item in payload["cpu_hash"]),
        "network": statistics.mean(cross_node),
    }


def aggregate(results):
    samples = [metric_values(result) for result in results]
    return {
        name: round(statistics.median(sample[name] for sample in samples), 3) for name in samples[0]
    }


def delta_percent(current, control):
    if control == 0:
        return 0.0
    return round(((current - control) / control) * 100, 2)


def evaluate(control, experiment, profile, max_regression, min_improvement):
    deltas = {name: delta_percent(experiment[name], control[name]) for name in control}
    reasons = []
    for name in ("api", "startup", "dns", "cpu"):
        if deltas[name] > max_regression:
            reasons.append(f"{name} regressed by {deltas[name]:.2f}%")
    if deltas["network"] < -max_regression:
        reasons.append(f"network regressed by {-deltas['network']:.2f}%")

    primary = "dns" if profile == "node-local-dns" else "cpu"
    if deltas[primary] > -min_improvement:
        reasons.append(
            f"{primary} improved by only {-deltas[primary]:.2f}% (required {min_improvement:.2f}%)"
        )
    return not reasons, deltas, reasons


def baseline_blockers(baselines):
    return [
        finding
        for baseline in baselines
        for finding in baseline.get("findings", [])
        if finding.get("severity") in {"warn", "fail"}
    ]


def maximum_temperature(baseline):
    values = [
        node.get("maximum_temperature_celsius")
        for node in baseline.get("nodes", [])
        if node.get("maximum_temperature_celsius") is not None
    ]
    return max(values) if values else None


def median_temperature(baselines):
    values = [value for value in map(maximum_temperature, baselines) if value is not None]
    return statistics.median(values) if values else None


def postflight_reasons(before, after, max_temperature_increase):
    reasons = [
        f"postflight {item['scope']}: {item['message']}" for item in baseline_blockers([after])
    ]
    before_temp = median_temperature(before)
    after_temp = maximum_temperature(after)
    if (
        before_temp is not None
        and after_temp is not None
        and after_temp - before_temp > max_temperature_increase
    ):
        reasons.append(
            f"temperature increased by {after_temp - before_temp:.2f} C "
            f"(maximum {max_temperature_increase:.2f} C)"
        )
    return reasons


def node_local_dns_enabled():
    result = subprocess.run(
        [
            "kubectl",
            f"--kubeconfig={KUBECONFIG}",
            "get",
            "daemonset",
            "node-local-dns",
            "-n",
            "kube-system",
        ],
        cwd=WORKDIR,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def cpu_governor():
    result = subprocess.run(
        [
            "ansible",
            "cluster",
            "-i",
            str(INVENTORY),
            "-m",
            "ansible.builtin.command",
            "-a",
            "cat /sys/devices/system/cpu/cpufreq/policy0/scaling_governor",
            "--one-line",
        ],
        cwd=WORKDIR,
        check=True,
        capture_output=True,
        text=True,
    )
    governors = {
        line.rsplit(" ", 1)[-1].strip() for line in result.stdout.splitlines() if line.strip()
    }
    if len(governors) != 1 or not governors.issubset({"ondemand", "schedutil"}):
        raise RuntimeError(f"Nodes have unsupported or mixed CPU governors: {sorted(governors)}")
    return governors.pop()


def set_profile(profile, state):
    if profile == "node-local-dns":
        playbook("29-manage-node-local-dns.yml", {"node_local_dns_state": state})
    else:
        playbook("28-apply-performance-profile.yml", {"performance_tuning_profile": state})


def write_report(run_id, payload):
    run_dir = OUTPUTS / "optimizations" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "result.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    lines = [
        f"# Cluster optimization: {run_id}",
        "",
        f"- Mode: `{payload['mode']}`",
        f"- Experiment: `{payload.get('experiment') or 'none'}`",
        f"- Decision: `{payload['decision']}`",
    ]
    for title, key in (
        ("Current medians", "current_medians"),
        ("Control medians", "control_medians"),
        ("Experiment medians", "experiment_medians"),
    ):
        if payload.get(key):
            lines.extend(["", f"## {title}", "", "| Metric | Value |", "| --- | ---: |"])
            lines.extend(f"| {name} | {value} |" for name, value in payload[key].items())
    if payload.get("deltas_percent"):
        lines.extend(["", "## Median deltas", "", "| Metric | Delta |", "| --- | ---: |"])
        lines.extend(
            f"| {name} | {value:+.2f}% |" for name, value in payload["deltas_percent"].items()
        )
    if payload.get("reasons"):
        lines.extend(["", "## Reasons", ""])
        lines.extend(f"- {reason}" for reason in payload["reasons"])
    (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Optimization report: {run_dir / 'report.md'}")


def execute(args):
    run_ids = RunIds()
    optimization_id = run_ids.next()
    playbook("31-maintenance-audit.yml")
    before = [capture_baseline(run_ids) for _ in range(args.baseline_runs)]
    blockers = baseline_blockers(before)
    if blockers:
        reasons = sorted({f"{item['scope']}: {item['message']}" for item in blockers})
        write_report(
            optimization_id,
            {
                "schema_version": 1,
                "mode": args.mode,
                "experiment": args.experiment,
                "decision": "blocked-by-preflight",
                "reasons": reasons,
            },
        )
        raise RuntimeError("Optimization blocked by baseline health findings")

    if args.mode == "audit":
        current = [capture_benchmark(run_ids, "audit-current") for _ in range(args.benchmark_runs)]
        after = capture_baseline(run_ids)
        reasons = postflight_reasons(before, after, args.max_temperature_increase)
        write_report(
            optimization_id,
            {
                "schema_version": 1,
                "mode": "audit",
                "experiment": None,
                "decision": "audit-complete" if not reasons else "failed-postflight",
                "current_medians": aggregate(current),
                "reasons": reasons,
            },
        )
        if reasons:
            raise RuntimeError("Optimization audit failed postflight health checks")
        return

    initial_dns = node_local_dns_enabled() if args.experiment == "node-local-dns" else None
    initial_governor = cpu_governor() if args.experiment == "schedutil" else None
    mutation_started = False
    promoted = False
    report_written = False
    try:
        mutation_started = True
        if args.experiment == "node-local-dns":
            set_profile(args.experiment, "absent")
        else:
            set_profile(args.experiment, "control")
        control = [capture_benchmark(run_ids, "control") for _ in range(args.benchmark_runs)]

        target_state = "enabled" if args.experiment == "node-local-dns" else args.experiment
        set_profile(args.experiment, target_state)
        measured = [capture_benchmark(run_ids, args.experiment) for _ in range(args.benchmark_runs)]
        after = capture_baseline(run_ids)
        control_metrics = aggregate(control)
        experiment_metrics = aggregate(measured)
        accepted, deltas, reasons = evaluate(
            control_metrics,
            experiment_metrics,
            args.experiment,
            args.max_regression_percent,
            args.min_primary_improvement_percent,
        )
        reasons.extend(postflight_reasons(before, after, args.max_temperature_increase))
        accepted = accepted and not reasons
        promoted = accepted
        write_report(
            optimization_id,
            {
                "schema_version": 1,
                "mode": "apply",
                "experiment": args.experiment,
                "decision": "promoted" if accepted else "rolled-back",
                "control_medians": control_metrics,
                "experiment_medians": experiment_metrics,
                "deltas_percent": deltas,
                "reasons": reasons,
            },
        )
        report_written = True
    except Exception as error:
        if not report_written:
            write_report(
                optimization_id,
                {
                    "schema_version": 1,
                    "mode": "apply",
                    "experiment": args.experiment,
                    "decision": "failed-error",
                    "reasons": [f"{type(error).__name__}: {error}"],
                },
            )
        raise
    finally:
        if mutation_started and not promoted:
            if args.experiment == "node-local-dns":
                set_profile(args.experiment, "enabled" if initial_dns else "absent")
            else:
                restore_profile = "schedutil" if initial_governor == "schedutil" else "control"
                set_profile(args.experiment, restore_profile)


def bounded_integer(minimum, maximum):
    def parse(value):
        result = int(value)
        if not minimum <= result <= maximum:
            raise argparse.ArgumentTypeError(f"must be between {minimum} and {maximum}")
        return result

    return parse


def main():
    parser = argparse.ArgumentParser(description="Safely audit and optimize a Raspberry Pi cluster")
    parser.add_argument("--mode", choices=("audit", "apply"), default="audit")
    parser.add_argument("--experiment", choices=("node-local-dns", "schedutil"))
    parser.add_argument("--baseline-runs", type=bounded_integer(2, 5), default=3)
    parser.add_argument("--benchmark-runs", type=bounded_integer(2, 5), default=3)
    parser.add_argument("--max-regression-percent", type=float, default=10.0)
    parser.add_argument("--min-primary-improvement-percent", type=float, default=5.0)
    parser.add_argument("--max-temperature-increase", type=float, default=5.0)
    args = parser.parse_args()
    if args.mode == "apply" and not args.experiment:
        parser.error("--experiment is required in apply mode")
    if args.mode == "audit" and args.experiment:
        parser.error("--experiment is only valid in apply mode")
    execute(args)


if __name__ == "__main__":
    main()
