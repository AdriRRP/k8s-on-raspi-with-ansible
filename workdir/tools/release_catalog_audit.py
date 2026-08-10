#!/usr/bin/env python3
"""Compare the validated platform catalog with authoritative upstream releases."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

KUBERNETES_STABLE_URL = "https://dl.k8s.io/release/stable.txt"
GITHUB_API = "https://api.github.com"


@dataclass(frozen=True)
class ReleaseCheck:
    name: str
    catalog_path: tuple[str, ...]
    repository: str
    release_tag_pattern: str = r"^v?\d+\.\d+\.\d+$"
    use_tags: bool = False


RELEASE_CHECKS = (
    ReleaseCheck("containerd", ("runtime", "containerd_version"), "containerd/containerd"),
    ReleaseCheck("runc", ("runtime", "runc_version"), "opencontainers/runc"),
    ReleaseCheck("CNI plugins", ("runtime", "cni_plugins_version"), "containernetworking/plugins"),
    ReleaseCheck("Calico", ("networking", "calico_version"), "projectcalico/calico"),
    ReleaseCheck(
        "MetalLB",
        ("networking", "metallb_version"),
        "metallb/metallb",
        use_tags=True,
    ),
    ReleaseCheck("Prometheus", ("observability", "prometheus_image"), "prometheus/prometheus"),
    ReleaseCheck("Grafana", ("observability", "grafana_image"), "grafana/grafana"),
    ReleaseCheck(
        "node-exporter",
        ("observability", "node_exporter_image"),
        "prometheus/node_exporter",
    ),
    ReleaseCheck("metrics-server", (), "kubernetes-sigs/metrics-server"),
    ReleaseCheck("kube-state-metrics", (), "kubernetes/kube-state-metrics"),
    ReleaseCheck("registry", ("storage", "registry_image"), "project-zot/zot"),
)


def normalize_version(value: str) -> str:
    value = value.strip()
    if "@" in value:
        value = value.split("@", 1)[0]
    if ":" in value:
        value = value.rsplit(":", 1)[1]
    return value.removeprefix("v")


def nested_value(mapping: dict[str, Any], path: tuple[str, ...]) -> str:
    value: Any = mapping
    for key in path:
        value = value[key]
    if not isinstance(value, str):
        raise TypeError(f"Catalog value at {'.'.join(path)} is not a string")
    return value


def request(url: str, timeout: float) -> bytes:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "release-catalog-audit"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    upstream_request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(upstream_request, timeout=timeout) as response:
        return response.read()


def latest_github_release(repository: str, pattern: str, timeout: float) -> str:
    releases = json.loads(request(f"{GITHUB_API}/repos/{repository}/releases?per_page=30", timeout))
    matcher = re.compile(pattern)
    tags = []
    for release in releases:
        tag = str(release.get("tag_name", ""))
        if not release.get("draft") and not release.get("prerelease") and matcher.fullmatch(tag):
            tags.append(tag)
    if not tags:
        raise RuntimeError(f"No stable semantic release found for {repository}")
    return max(tags, key=semantic_version)


def semantic_version(tag: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag)
    if not match:
        raise ValueError(f"Not a semantic version: {tag}")
    return tuple(int(component) for component in match.groups())


def latest_github_tag(repository: str, pattern: str, timeout: float) -> str:
    tags = json.loads(request(f"{GITHUB_API}/repos/{repository}/tags?per_page=100", timeout))
    matcher = re.compile(pattern)
    stable_tags = [str(tag["name"]) for tag in tags if matcher.fullmatch(str(tag["name"]))]
    if not stable_tags:
        raise RuntimeError(f"No stable semantic tag found for {repository}")
    return max(stable_tags, key=semantic_version)


def expected_value(catalog: dict[str, Any], check: ReleaseCheck) -> str:
    if check.catalog_path:
        return nested_value(catalog, check.catalog_path)

    kubernetes = catalog["kubernetes"]
    minor = ".".join(str(kubernetes["install_version"]).split(".")[:2])
    image_map_name = {
        "metrics-server": "metrics_server_images",
        "kube-state-metrics": "kube_state_metrics_images",
    }[check.name]
    return nested_value(catalog, ("observability", image_map_name, minor))


def audit(catalog: dict[str, Any], timeout: float) -> list[tuple[str, str, str]]:
    stale: list[tuple[str, str, str]] = []
    kubernetes = catalog["kubernetes"]
    expected_kubernetes = normalize_version(str(kubernetes["latest_upstream_version"]))
    actual_kubernetes = normalize_version(request(KUBERNETES_STABLE_URL, timeout).decode())
    if expected_kubernetes != actual_kubernetes:
        stale.append(("Kubernetes", expected_kubernetes, actual_kubernetes))

    for check in RELEASE_CHECKS:
        expected = normalize_version(expected_value(catalog, check))
        latest = latest_github_tag if check.use_tags else latest_github_release
        actual = normalize_version(latest(check.repository, check.release_tag_pattern, timeout))
        if expected != actual:
            stale.append((check.name, expected, actual))
    return stale


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("inventory/group_vars/all.yml"),
        help="Path to the Ansible variables file",
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout in seconds")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        document = yaml.safe_load(args.catalog.read_text())
        catalog = document["platform_release_catalog"]
        stale = audit(catalog, args.timeout)
    except (OSError, KeyError, TypeError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"Release catalog audit failed: {error}", file=sys.stderr)
        return 2

    if stale:
        print("Release catalog updates are available:")
        for name, current, latest in stale:
            print(f"- {name}: {current} -> {latest}")
        return 1

    print("Release catalog matches all audited stable upstream releases.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
