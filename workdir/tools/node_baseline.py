#!/usr/bin/env python3

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def run(command, timeout):
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {
            "available": False,
            "returncode": None,
            "stdout": "",
            "stderr": str(error),
        }
    return {
        "available": True,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def parse_os_release():
    values = {}
    for line in read_text("/etc/os-release").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    return values


def parse_meminfo():
    values = {}
    for line in read_text("/proc/meminfo").splitlines():
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        parts = raw_value.split()
        if not parts:
            continue
        try:
            value = int(parts[0])
        except ValueError:
            continue
        values[key] = value * 1024 if len(parts) > 1 and parts[1] == "kB" else value
    return values


def parse_pressure(path):
    pressure = {}
    for line in read_text(path).splitlines():
        parts = line.split()
        if not parts:
            continue
        values = {}
        for item in parts[1:]:
            if "=" not in item:
                continue
            key, raw_value = item.split("=", 1)
            try:
                values[key] = int(raw_value) if key == "total" else float(raw_value)
            except ValueError:
                values[key] = raw_value
        pressure[parts[0]] = values
    return pressure


def parse_cpu_info():
    values = {}
    wanted = {"model name", "Model", "Hardware", "Revision"}
    for line in read_text("/proc/cpuinfo").splitlines():
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        if key in wanted and key not in values:
            values[key] = value
    return values


def parse_temperatures():
    temperatures = []
    thermal_root = Path("/sys/class/thermal")
    for zone in sorted(thermal_root.glob("thermal_zone*")):
        raw_temperature = read_text(zone / "temp")
        try:
            temperature = float(raw_temperature)
        except ValueError:
            continue
        if temperature > 1000:
            temperature /= 1000
        temperatures.append(
            {
                "zone": zone.name,
                "type": read_text(zone / "type") or "unknown",
                "celsius": round(temperature, 2),
            }
        )
    return temperatures


def parse_cpu_frequencies():
    policies = []
    for policy in sorted(Path("/sys/devices/system/cpu/cpufreq").glob("policy*")):
        values = {"policy": policy.name}
        for name in (
            "scaling_governor",
            "scaling_cur_freq",
            "scaling_min_freq",
            "scaling_max_freq",
            "cpuinfo_max_freq",
        ):
            value = read_text(policy / name)
            if not value:
                continue
            if name.endswith("_freq"):
                try:
                    values[f"{name}_hz"] = int(value) * 1000
                except ValueError:
                    values[name] = value
            else:
                values[name] = value
        policies.append(values)
    return policies


def parse_diskstats():
    devices = {}
    for line in read_text("/proc/diskstats").splitlines():
        parts = line.split()
        if len(parts) < 14:
            continue
        try:
            devices[parts[2]] = {
                "reads_completed": int(parts[3]),
                "sectors_read": int(parts[5]),
                "read_time_ms": int(parts[6]),
                "writes_completed": int(parts[7]),
                "sectors_written": int(parts[9]),
                "write_time_ms": int(parts[10]),
                "io_in_progress": int(parts[11]),
                "io_time_ms": int(parts[12]),
                "weighted_io_time_ms": int(parts[13]),
            }
        except ValueError:
            continue
    return devices


def parse_network_counters():
    interfaces = {}
    for line in read_text("/proc/net/dev").splitlines()[2:]:
        if ":" not in line:
            continue
        name, raw_counters = line.split(":", 1)
        counters = raw_counters.split()
        if len(counters) < 16:
            continue
        interfaces[name.strip()] = {
            "rx_bytes": int(counters[0]),
            "rx_packets": int(counters[1]),
            "rx_errors": int(counters[2]),
            "rx_dropped": int(counters[3]),
            "tx_bytes": int(counters[8]),
            "tx_packets": int(counters[9]),
            "tx_errors": int(counters[10]),
            "tx_dropped": int(counters[11]),
        }
    return interfaces


def interface_properties():
    interfaces = {}
    for interface in sorted(Path("/sys/class/net").iterdir()):
        if interface.name == "lo":
            continue
        values = {
            "operstate": read_text(interface / "operstate"),
            "mtu": read_text(interface / "mtu"),
            "speed_mbps": read_text(interface / "speed"),
            "duplex": read_text(interface / "duplex"),
        }
        interfaces[interface.name] = {key: value for key, value in values.items() if value}
    return interfaces


def parse_filesystems(timeout):
    result = run(
        ["df", "-B1", "--output=source,fstype,size,used,avail,pcent,target"],
        timeout,
    )
    filesystems = []
    if result["returncode"] != 0:
        return filesystems, result
    for line in result["stdout"].splitlines()[1:]:
        parts = line.split(maxsplit=6)
        if len(parts) != 7:
            continue
        try:
            size = int(parts[2])
            used = int(parts[3])
            available = int(parts[4])
            used_percent = float(parts[5].rstrip("%"))
        except ValueError:
            continue
        filesystems.append(
            {
                "source": parts[0],
                "fstype": parts[1],
                "size_bytes": size,
                "used_bytes": used,
                "available_bytes": available,
                "used_percent": used_percent,
                "mountpoint": parts[6],
            }
        )
    return filesystems, result


def path_sizes(timeout):
    sizes = {}
    for path in (
        "/var/lib/containerd",
        "/var/lib/kubelet",
        "/var/lib/etcd",
        "/var/log",
        "/var/cache/apt",
    ):
        if not Path(path).exists():
            continue
        result = run(["du", "-x", "-s", "-B1", path], timeout)
        if result["returncode"] != 0 or not result["stdout"]:
            sizes[path] = {"error": result["stderr"] or "du failed"}
            continue
        try:
            sizes[path] = {"bytes": int(result["stdout"].split()[0])}
        except (IndexError, ValueError):
            sizes[path] = {"error": "unexpected du output"}
    return sizes


def service_states(timeout):
    services = {}
    for service in ("containerd", "kubelet"):
        result = run(["systemctl", "is-active", service], timeout)
        services[service] = result["stdout"] or "unknown"
    failed = run(
        ["systemctl", "--failed", "--no-legend", "--plain", "--no-pager"],
        timeout,
    )
    return services, [line for line in failed["stdout"].splitlines() if line.strip()]


def software_versions(timeout):
    commands = {
        "kubelet": ["kubelet", "--version"],
        "containerd": ["containerd", "--version"],
        "runc": ["runc", "--version"],
    }
    versions = {}
    for name, command in commands.items():
        result = run(command, timeout)
        versions[name] = {
            "available": result["available"],
            "returncode": result["returncode"],
            "version": result["stdout"].splitlines()[0] if result["stdout"] else "",
        }
    return versions


def collect(timeout):
    memory = parse_meminfo()
    filesystems, filesystem_command = parse_filesystems(timeout)
    service_status, failed_units = service_states(timeout)
    load_average = os.getloadavg()
    uptime_raw = read_text("/proc/uptime").split()
    uptime_seconds = float(uptime_raw[0]) if uptime_raw else 0
    throttling = (
        run(["vcgencmd", "get_throttled"], timeout)
        if shutil.which("vcgencmd")
        else {
            "available": False,
            "returncode": None,
            "stdout": "",
            "stderr": "vcgencmd not installed",
        }
    )
    processes = run(
        ["ps", "-eo", "comm,pid,pcpu,pmem,rss,vsz", "--sort=-pcpu"],
        timeout,
    )
    journal = run(["journalctl", "--disk-usage", "--no-pager"], timeout)
    block_devices = run(
        [
            "lsblk",
            "--json",
            "--bytes",
            "--output",
            "NAME,KNAME,TYPE,SIZE,ROTA,TRAN,FSTYPE,MOUNTPOINTS",
        ],
        timeout,
    )

    return {
        "schema_version": 1,
        "profile": "observe",
        "captured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "node": {
            "hostname": socket.gethostname(),
            "architecture": platform.machine(),
            "kernel": platform.release(),
            "os_release": parse_os_release(),
            "boot_id": read_text("/proc/sys/kernel/random/boot_id"),
            "uptime_seconds": uptime_seconds,
        },
        "cpu": {
            "logical_cpu_count": os.cpu_count(),
            "details": parse_cpu_info(),
            "load_average": {
                "one_minute": load_average[0],
                "five_minutes": load_average[1],
                "fifteen_minutes": load_average[2],
            },
            "frequency_policies": parse_cpu_frequencies(),
            "temperatures": parse_temperatures(),
            "raspberry_pi_throttling": throttling,
        },
        "memory": {
            "total_bytes": memory.get("MemTotal", 0),
            "available_bytes": memory.get("MemAvailable", 0),
            "free_bytes": memory.get("MemFree", 0),
            "cached_bytes": memory.get("Cached", 0),
            "dirty_bytes": memory.get("Dirty", 0),
            "writeback_bytes": memory.get("Writeback", 0),
            "swap_total_bytes": memory.get("SwapTotal", 0),
            "swap_free_bytes": memory.get("SwapFree", 0),
        },
        "pressure": {
            "cpu": parse_pressure("/proc/pressure/cpu"),
            "memory": parse_pressure("/proc/pressure/memory"),
            "io": parse_pressure("/proc/pressure/io"),
        },
        "storage": {
            "filesystems": filesystems,
            "filesystems_command": filesystem_command,
            "block_devices": (
                json.loads(block_devices["stdout"])
                if block_devices["returncode"] == 0 and block_devices["stdout"]
                else {"error": block_devices["stderr"] or "lsblk failed"}
            ),
            "diskstats": parse_diskstats(),
            "path_sizes": path_sizes(timeout),
        },
        "network": {
            "counters": parse_network_counters(),
            "interfaces": interface_properties(),
        },
        "services": {
            "states": service_status,
            "failed_units": failed_units,
            "journal_disk_usage": journal["stdout"],
        },
        "software": software_versions(timeout),
        "top_processes": processes["stdout"].splitlines()[:26],
    }


def main():
    parser = argparse.ArgumentParser(description="Capture a read-only Linux node baseline.")
    parser.add_argument("--timeout", type=int, default=15)
    args = parser.parse_args()
    if args.timeout < 1 or args.timeout > 120:
        parser.error("--timeout must be between 1 and 120 seconds")
    print(json.dumps(collect(args.timeout), sort_keys=True))


if __name__ == "__main__":
    main()
