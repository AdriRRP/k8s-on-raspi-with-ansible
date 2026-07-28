#!/usr/bin/env python3

import argparse
import ipaddress
import json
import os
import shlex
import socket
import subprocess
import sys
from pathlib import Path

DEFAULT_KUBECONFIG = "/home/ansible/.kube/config"
DEFAULT_SSH_KEY = "/home/ansible/.ssh/id_ed25519"
DEFAULT_STATIC_INVENTORY = "/home/ansible/workdir/inventory/hosts.ini"
DEFAULT_WRITE_PATH = os.environ.get("DISCOVERY_WRITE_PATH", "")
MAX_SCAN_ADDRESSES = 1024


def run_command(command, check=True):
    return subprocess.run(
        command,
        check=check,
        capture_output=True,
        text=True,
    )


def command_exists(command_name):
    return (
        subprocess.run(
            ["bash", "-lc", f"command -v {shlex.quote(command_name)} >/dev/null 2>&1"],
            check=False,
        ).returncode
        == 0
    )


def first_non_empty(*values):
    for value in values:
        if value:
            return value
    return ""


def build_inventory(hosts, source, ssh_user, ssh_port, kubeconfig_path):
    inventory = {
        "_meta": {
            "hostvars": {
                "localhost": {
                    "ansible_connection": "local",
                    "kubeconfig_local_path": kubeconfig_path,
                    "cluster_discovery_source": source,
                }
            }
        },
        "all": {
            "children": ["local", "control_plane", "master", "workers", "cluster"],
            "vars": {
                "cluster_discovery_source": source,
                "cluster_discovery_ssh_user": ssh_user,
                "cluster_discovery_ssh_port": ssh_port,
                "kubeconfig_local_path": kubeconfig_path,
            },
        },
        "local": {"hosts": ["localhost"]},
        "control_plane": {"hosts": []},
        "master": {"hosts": []},
        "workers": {"hosts": []},
        "cluster": {"hosts": []},
    }

    control_plane_hosts = []
    worker_hosts = []

    for host in hosts:
        name = host["name"]
        inventory["_meta"]["hostvars"][name] = {
            "ansible_host": host["ansible_host"],
            "ansible_user": host.get("ansible_user", ssh_user),
            "ansible_port": host.get("ansible_port", ssh_port),
            "kube_node_name": host.get("kube_node_name", name),
            "cluster_discovery_source": source,
        }
        inventory["cluster"]["hosts"].append(name)
        if host["is_control_plane"]:
            control_plane_hosts.append(name)
        else:
            worker_hosts.append(name)

    inventory["control_plane"]["hosts"] = control_plane_hosts
    inventory["master"]["hosts"] = control_plane_hosts
    inventory["workers"]["hosts"] = worker_hosts
    return inventory


def discover_from_kubeconfig(kubeconfig_path, ssh_user, ssh_port):
    kubeconfig = Path(kubeconfig_path)
    if not kubeconfig.exists():
        return None
    if not command_exists("kubectl"):
        return None

    result = run_command(
        [
            "kubectl",
            "get",
            "nodes",
            "-o",
            "json",
            f"--kubeconfig={kubeconfig_path}",
        ],
        check=False,
    )
    if result.returncode != 0:
        return None

    payload = json.loads(result.stdout)
    hosts = []

    for item in payload.get("items", []):
        metadata = item.get("metadata", {})
        status = item.get("status", {})
        labels = metadata.get("labels", {})
        addresses = status.get("addresses", [])
        internal_ip = ""
        external_ip = ""

        for address in addresses:
            if address.get("type") == "InternalIP" and not internal_ip:
                internal_ip = address.get("address", "")
            if address.get("type") == "ExternalIP" and not external_ip:
                external_ip = address.get("address", "")

        host_ip = first_non_empty(internal_ip, external_ip)
        if not host_ip:
            continue

        is_control_plane = any(
            label_key.startswith("node-role.kubernetes.io/control-plane")
            or label_key.startswith("node-role.kubernetes.io/master")
            for label_key in labels.keys()
        )

        hosts.append(
            {
                "name": metadata.get("name", host_ip.replace(".", "-")),
                "ansible_host": host_ip,
                "ansible_user": ssh_user,
                "ansible_port": ssh_port,
                "kube_node_name": metadata.get("name", host_ip.replace(".", "-")),
                "is_control_plane": is_control_plane,
            }
        )

    if not hosts:
        return None

    return build_inventory(hosts, "kubeconfig", ssh_user, ssh_port, kubeconfig_path)


def parse_static_inventory(
    static_inventory_path,
    fallback_ssh_user,
    fallback_ssh_port,
    kubeconfig_path,
):
    inventory_path = Path(static_inventory_path)
    if not inventory_path.exists():
        return None

    current_group = None
    hosts = []

    for raw_line in inventory_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_group = line[1:-1]
            continue
        if current_group in {"master", "workers"}:
            parts = line.split()
            if not parts:
                continue
            name = parts[0]
            hostvars = {}
            for part in parts[1:]:
                if "=" not in part:
                    continue
                key, value = part.split("=", 1)
                hostvars[key] = value
            hosts.append(
                {
                    "name": name,
                    "ansible_host": hostvars.get("ansible_host", name),
                    "ansible_user": hostvars.get("ansible_user", fallback_ssh_user),
                    "ansible_port": int(hostvars.get("ansible_port", fallback_ssh_port)),
                    "kube_node_name": name,
                    "is_control_plane": current_group == "master",
                }
            )

    if not hosts:
        return None

    return build_inventory(
        hosts,
        "static_inventory",
        fallback_ssh_user,
        fallback_ssh_port,
        kubeconfig_path,
    )


def port_open(host, port, timeout):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ssh_probe(host, ssh_user, ssh_key, ssh_port, connect_timeout):
    ssh_base = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={int(connect_timeout)}",
        "-i",
        ssh_key,
        "-p",
        str(ssh_port),
        f"{ssh_user}@{host}",
    ]

    hostname_cmd = ssh_base + ["hostname", "-s"]
    hostname_result = run_command(hostname_cmd, check=False)
    if hostname_result.returncode != 0:
        return None

    control_plane_cmd = ssh_base + ["test", "-f", "/etc/kubernetes/manifests/kube-apiserver.yaml"]
    control_plane_result = run_command(control_plane_cmd, check=False)

    hostname = hostname_result.stdout.strip()
    if not hostname:
        hostname = host.replace(".", "-")

    return {
        "name": hostname,
        "ansible_host": host,
        "ansible_user": ssh_user,
        "ansible_port": ssh_port,
        "kube_node_name": hostname,
        "is_control_plane": control_plane_result.returncode == 0,
    }


def validate_scan_network(scan_cidr):
    try:
        network = ipaddress.ip_network(scan_cidr, strict=False)
    except ValueError as exc:
        raise RuntimeError(f"Invalid discovery CIDR: {scan_cidr}") from exc

    if network.version != 4:
        raise RuntimeError("Network discovery currently supports IPv4 CIDRs only.")
    if not network.is_private:
        raise RuntimeError(f"Refusing to scan non-private network {network}.")
    if network.num_addresses > MAX_SCAN_ADDRESSES:
        raise RuntimeError(
            f"Refusing to scan {network.num_addresses} addresses; "
            f"the safety limit is {MAX_SCAN_ADDRESSES}."
        )
    return network


def discover_from_scan(scan_cidr, ssh_user, ssh_key, ssh_port, connect_timeout, kubeconfig_path):
    if not scan_cidr:
        return None

    network = validate_scan_network(scan_cidr)
    hosts = []

    for candidate in network.hosts():
        candidate_ip = str(candidate)
        if not port_open(candidate_ip, ssh_port, connect_timeout):
            continue
        host_record = ssh_probe(candidate_ip, ssh_user, ssh_key, ssh_port, connect_timeout)
        if host_record:
            hosts.append(host_record)

    if not hosts:
        return None

    return build_inventory(hosts, "scan", ssh_user, ssh_port, kubeconfig_path)


def discover_inventory(args):
    if args.strategy in {"auto", "kubeconfig"}:
        inventory = discover_from_kubeconfig(args.kubeconfig, args.ssh_user, args.ssh_port)
        if inventory:
            return inventory
        if args.strategy == "kubeconfig":
            raise RuntimeError(f"Unable to discover cluster from kubeconfig at {args.kubeconfig}.")

    if args.strategy in {"auto", "scan"} and args.cidr:
        inventory = discover_from_scan(
            args.cidr,
            args.ssh_user,
            args.ssh_key,
            args.ssh_port,
            args.connect_timeout,
            args.kubeconfig,
        )
        if inventory:
            return inventory
        if args.strategy == "scan":
            raise RuntimeError(f"Unable to discover cluster nodes by scanning {args.cidr}.")

    if args.strategy in {"auto", "static"}:
        inventory = parse_static_inventory(
            args.static_inventory,
            args.ssh_user,
            args.ssh_port,
            args.kubeconfig,
        )
        if inventory:
            return inventory
        if args.strategy == "static":
            raise RuntimeError(f"Unable to read usable hosts from {args.static_inventory}.")

    raise RuntimeError(
        "Unable to discover cluster inventory. Provide a valid kubeconfig, "
        "a scan CIDR or a usable static inventory file."
    )


def parse_args():
    parser = argparse.ArgumentParser(
        description="Discover a Kubernetes homelab cluster and emit Ansible inventory JSON."
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Emit inventory JSON for Ansible dynamic inventory.",
    )
    parser.add_argument(
        "--host",
        default="",
        help="Required by Ansible inventory plugin interface.",
    )
    parser.add_argument(
        "--write",
        default="",
        help="Optional path where the discovered inventory JSON should be written.",
    )
    parser.add_argument(
        "--strategy",
        choices=["auto", "kubeconfig", "scan", "static"],
        default=os.environ.get("DISCOVERY_STRATEGY", "auto"),
        help="Discovery strategy preference.",
    )
    parser.add_argument(
        "--kubeconfig",
        default=os.environ.get("DISCOVERY_KUBECONFIG", DEFAULT_KUBECONFIG),
        help="Path to kubeconfig used for cluster API discovery.",
    )
    parser.add_argument(
        "--cidr",
        default=os.environ.get("DISCOVERY_SCAN_CIDR", ""),
        help="Optional local network CIDR used for SSH discovery.",
    )
    parser.add_argument(
        "--ssh-user",
        default=os.environ.get("DISCOVERY_SSH_USER", os.environ.get("CLUSTER_ADMIN_USER", "admin")),
        help="SSH user used for discovery and generated inventory.",
    )
    parser.add_argument(
        "--ssh-key",
        default=os.environ.get("DISCOVERY_SSH_KEY", DEFAULT_SSH_KEY),
        help="SSH private key used during network scan discovery.",
    )
    parser.add_argument(
        "--ssh-port",
        type=int,
        default=int(os.environ.get("DISCOVERY_SSH_PORT", "22")),
        help="SSH port used during scan discovery.",
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=float(os.environ.get("DISCOVERY_CONNECT_TIMEOUT", "2")),
        help="TCP and SSH connect timeout in seconds for scan discovery.",
    )
    parser.add_argument(
        "--static-inventory",
        default=os.environ.get("DISCOVERY_STATIC_INVENTORY", DEFAULT_STATIC_INVENTORY),
        help="Fallback static inventory file to parse when kubeconfig or scan are unavailable.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.host:
        print("{}")
        return 0

    try:
        inventory = discover_inventory(args)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    rendered = json.dumps(inventory, indent=2, sort_keys=True)
    output_target = args.write or DEFAULT_WRITE_PATH
    if output_target:
        output_path = Path(output_target)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")

    if args.list or not output_target:
        print(rendered)

    return 0


if __name__ == "__main__":
    sys.exit(main())
