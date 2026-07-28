#!/usr/bin/env python3
"""Prepare and flash Raspberry Pi node media from the control host."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import plistlib
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from typing import Any

REQUIRED_SEED_FILES = ("user-data", "meta-data", "network-config")


def run_command(
    argv: list[str],
    *,
    capture_output: bool = True,
    check: bool = True,
    text: bool = False,
) -> subprocess.CompletedProcess[bytes] | subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        check=False,
        capture_output=capture_output,
        text=text,
    )
    if check and result.returncode != 0:
        stderr = result.stderr if text else (result.stderr or b"").decode()
        stdout = result.stdout if text else (result.stdout or b"").decode()
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(argv)}\n"
            f"stdout:\n{stdout}\n"
            f"stderr:\n{stderr}"
        )
    return result


def load_plist_command(argv: list[str]) -> dict[str, Any]:
    result = run_command(argv, text=False)
    return plistlib.loads(result.stdout)


def normalize_device(device: str) -> str:
    candidate = os.path.basename(device)
    if not re.fullmatch(r"disk\d+", candidate):
        raise ValueError(f"Expected a whole-disk device like /dev/disk4, got: {device}")
    return f"/dev/{candidate}"


def raw_device(device_node: str) -> str:
    return f"/dev/r{os.path.basename(device_node)}"


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_sha256sums(contents: str, filename: str) -> str:
    for raw_line in contents.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        checksum = parts[0]
        listed_name = parts[-1].lstrip("*")
        if listed_name == filename:
            return checksum
    raise ValueError(f"Could not find checksum for {filename} in SHA256SUMS")


def ensure_parent(path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def download_file(url: str, destination: pathlib.Path) -> None:
    ensure_parent(destination)
    with urllib.request.urlopen(url) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def disk_records() -> list[dict[str, Any]]:
    listing = load_plist_command(["diskutil", "list", "-plist"])
    records: list[dict[str, Any]] = []
    for disk in listing.get("WholeDisks", []):
        info = load_plist_command(["diskutil", "info", "-plist", f"/dev/{disk}"])
        records.append(summarize_disk(info))
    return records


def summarize_disk(info: dict[str, Any]) -> dict[str, Any]:
    record = {
        "device_identifier": info.get("DeviceIdentifier", ""),
        "device_node": info.get("DeviceNode", ""),
        "raw_device_node": raw_device(info.get("DeviceNode", "/dev/invalid")),
        "media_name": info.get("MediaName", ""),
        "bus_protocol": info.get("BusProtocol", ""),
        "total_size_bytes": int(info.get("TotalSize", info.get("Size", 0)) or 0),
        "internal": bool(info.get("Internal", False)),
        "removable": bool(info.get("RemovableMediaOrExternalDevice", False)),
        "removable_media": bool(info.get("RemovableMedia", False)),
        "ejectable": bool(info.get("Ejectable", False)),
        "whole_disk": bool(info.get("WholeDisk", False)),
        "writable_media": bool(info.get("WritableMedia", False)),
        "virtual_or_physical": info.get("VirtualOrPhysical", ""),
    }
    record["candidate"] = is_safe_candidate(record)
    record["size_gib"] = round(record["total_size_bytes"] / (1024**3), 2)
    record["reasons"] = candidate_reasons(record)
    return record


def is_safe_candidate(record: dict[str, Any]) -> bool:
    return (
        record.get("whole_disk") is True
        and record.get("writable_media") is True
        and record.get("internal") is False
        and record.get("virtual_or_physical", "Physical") == "Physical"
        and (
            record.get("removable") is True
            or record.get("removable_media") is True
            or record.get("ejectable") is True
        )
    )


def candidate_reasons(record: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if not record.get("whole_disk"):
        reasons.append("not-a-whole-disk")
    if not record.get("writable_media"):
        reasons.append("not-writable")
    if record.get("internal"):
        reasons.append("internal-media")
    if record.get("virtual_or_physical") != "Physical":
        reasons.append(f"virtual-or-{record.get('virtual_or_physical', 'unknown').lower()}")
    if not (record.get("removable") or record.get("removable_media") or record.get("ejectable")):
        reasons.append("not-removable-or-external")
    if not reasons:
        reasons.append("candidate")
    return reasons


def command_list_disks(args: argparse.Namespace) -> int:
    records = disk_records()
    if args.json:
        print(json.dumps(records, indent=2, sort_keys=True))
        return 0

    candidates = [record for record in records if record["candidate"]]
    if not candidates:
        print("No removable whole-disk candidates detected by macOS diskutil.")
        print("If you just connected the drive, re-seat it and check it appears in Disk Utility.")
        return 0

    for record in candidates:
        print(
            f"{record['device_node']} ({record['raw_device_node']}) | "
            f"{record['size_gib']} GiB | {record['bus_protocol']} | {record['media_name']}"
        )
    return 0


def command_prepare_image(args: argparse.Namespace) -> int:
    image_cache_dir = pathlib.Path(args.image_cache_dir)
    manifest_path = pathlib.Path(args.manifest_path)
    seed_dir = pathlib.Path(args.seed_dir)
    image_url = args.image_url
    sha256sums_url = args.sha256sums_url
    image_name = pathlib.Path(urllib.parse.urlparse(image_url).path).name
    image_path = image_cache_dir / image_name
    sha256sums_path = (
        image_cache_dir / pathlib.Path(urllib.parse.urlparse(sha256sums_url).path).name
    )

    missing_seed_files = [
        seed_file for seed_file in REQUIRED_SEED_FILES if not (seed_dir / seed_file).is_file()
    ]
    if missing_seed_files:
        raise RuntimeError(
            f"Seed directory {seed_dir} is missing required files: " + ", ".join(missing_seed_files)
        )

    print(f"Preparing node media for {args.node}")
    print(f"Seed directory: {seed_dir}")
    print(f"Image URL: {image_url}")

    if args.refresh or not sha256sums_path.exists():
        print(f"Downloading SHA256SUMS to {sha256sums_path}")
        download_file(sha256sums_url, sha256sums_path)

    expected_sha256 = parse_sha256sums(sha256sums_path.read_text(), image_name)
    needs_download = args.refresh or not image_path.exists()

    if image_path.exists() and not needs_download:
        current_sha256 = sha256_file(image_path)
        if current_sha256 != expected_sha256:
            print(
                f"Cached image checksum mismatch for {image_path.name}; redownloading.",
                file=sys.stderr,
            )
            image_path.unlink()
            needs_download = True

    if needs_download:
        print(f"Downloading image to {image_path}")
        download_file(image_url, image_path)

    actual_sha256 = sha256_file(image_path)
    if actual_sha256 != expected_sha256:
        raise RuntimeError(
            f"Checksum mismatch for {image_path}: expected {expected_sha256}, got {actual_sha256}"
        )

    manifest = {
        "prepared_at_utc": utc_now(),
        "node": args.node,
        "image_url": image_url,
        "sha256sums_url": sha256sums_url,
        "image_path": str(image_path),
        "image_sha256": actual_sha256,
        "seed_dir": str(seed_dir),
        "bundle_id": args.bundle_id or "",
    }
    ensure_parent(manifest_path)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    print(f"Prepared manifest: {manifest_path}")
    print(f"Verified image SHA256: {actual_sha256}")
    return 0


def validate_seed_dir(seed_dir: pathlib.Path) -> None:
    missing = [
        seed_file for seed_file in REQUIRED_SEED_FILES if not (seed_dir / seed_file).is_file()
    ]
    if missing:
        raise RuntimeError(
            f"Seed directory {seed_dir} is missing required files: {', '.join(missing)}"
        )


def validate_target_device(device_node: str) -> dict[str, Any]:
    info = load_plist_command(["diskutil", "info", "-plist", device_node])
    record = summarize_disk(info)
    if not record["candidate"]:
        raise RuntimeError(
            f"Refusing to flash {device_node}. Safety checks failed: "
            + ", ".join(record["reasons"])
        )
    return record


def ensure_noninteractive_sudo() -> None:
    result = subprocess.run(
        ["sudo", "-n", "true"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "This flash flow requires non-interactive sudo for raw disk writes. "
            "Please run `sudo -v` in your terminal first, then rerun the flash command."
        )


def wait_for_partitions(device_node: str, timeout_seconds: int = 30) -> list[dict[str, Any]]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        listing = load_plist_command(["diskutil", "list", "-plist", device_node])
        entries = listing.get("AllDisksAndPartitions", [])
        if entries and entries[0].get("Partitions"):
            return list(entries[0]["Partitions"])
        time.sleep(1)
    raise RuntimeError(f"Timed out waiting for partitions to appear on {device_node}")


def choose_boot_partition(partitions: list[dict[str, Any]]) -> str:
    preferred_contents = ("DOS_FAT_32", "EFI", "Microsoft Basic Data")
    for content_name in preferred_contents:
        for partition in partitions:
            content = partition.get("Content", "")
            if content_name in content:
                return f"/dev/{partition['DeviceIdentifier']}"
    if partitions:
        return f"/dev/{partitions[0]['DeviceIdentifier']}"
    raise RuntimeError("No partitions found after flashing the image")


def mount_partition(partition_device: str) -> pathlib.Path:
    run_command(["diskutil", "mount", partition_device], capture_output=True, check=True)
    info = load_plist_command(["diskutil", "info", "-plist", partition_device])
    mount_point = info.get("MountPoint", "")
    if not mount_point:
        raise RuntimeError(f"Partition {partition_device} did not mount cleanly")
    return pathlib.Path(mount_point)


def copy_seed(seed_dir: pathlib.Path, mount_point: pathlib.Path) -> None:
    for seed_file in REQUIRED_SEED_FILES:
        source = seed_dir / seed_file
        destination = mount_point / seed_file
        shutil.copyfile(source, destination)
        if source.read_bytes() != destination.read_bytes():
            raise RuntimeError(f"Verification failed after copying {seed_file} to {destination}")


def command_flash_image(args: argparse.Namespace) -> int:
    device_node = normalize_device(args.device)
    image_path = pathlib.Path(args.image_path)
    seed_dir = pathlib.Path(args.seed_dir)
    manifest_path = pathlib.Path(args.manifest_path)

    if not image_path.is_file():
        raise RuntimeError(f"Image file not found: {image_path}")
    if not manifest_path.is_file():
        raise RuntimeError(f"Prepared manifest not found: {manifest_path}")
    if image_path.suffix != ".xz":
        raise RuntimeError(f"Expected an .xz-compressed image, got: {image_path.name}")
    if shutil.which("xz") is None:
        raise RuntimeError("xz is required on the host to stream the Ubuntu image")

    validate_seed_dir(seed_dir)
    record = validate_target_device(device_node)

    print(
        f"Flashing {record['device_node']} ({record['size_gib']} GiB, {record['media_name']}) "
        f"for node {args.node}"
    )
    print(f"Image: {image_path}")
    print(f"Seed: {seed_dir}")

    if args.dry_run:
        print("Dry-run enabled; stopping before any destructive operation.")
        return 0

    ensure_noninteractive_sudo()
    run_command(["diskutil", "unmountDisk", "force", device_node], text=True)
    run_command(
        [
            "sudo",
            "-n",
            "/bin/sh",
            "-c",
            'xz -dc "$1" | dd of="$2" bs=4m iflag=fullblock conv=fsync status=progress',
            "sh",
            str(image_path),
            raw_device(device_node),
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    run_command(["sync"], text=True)
    partitions = wait_for_partitions(device_node)
    boot_partition = choose_boot_partition(partitions)
    mount_point = mount_partition(boot_partition)
    copy_seed(seed_dir, mount_point)
    run_command(["diskutil", "unmount", boot_partition], text=True)
    run_command(["diskutil", "eject", device_node], text=True)

    print(f"Flashed {device_node} successfully.")
    print(f"Boot seed injected onto {boot_partition}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list-disks", help="List safe removable disks.")
    list_parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    list_parser.set_defaults(func=command_list_disks)

    prepare_parser = subparsers.add_parser(
        "prepare-image",
        help="Download and verify the Ubuntu image and store a node-media manifest.",
    )
    prepare_parser.add_argument("--node", required=True)
    prepare_parser.add_argument("--seed-dir", required=True)
    prepare_parser.add_argument("--image-url", required=True)
    prepare_parser.add_argument("--sha256sums-url", required=True)
    prepare_parser.add_argument("--image-cache-dir", required=True)
    prepare_parser.add_argument("--manifest-path", required=True)
    prepare_parser.add_argument("--bundle-id", default="")
    prepare_parser.add_argument("--refresh", action="store_true")
    prepare_parser.set_defaults(func=command_prepare_image)

    flash_parser = subparsers.add_parser(
        "flash-image",
        help=(
            "Flash the prepared Ubuntu image to a removable disk and inject cloud-init seed files."
        ),
    )
    flash_parser.add_argument("--node", required=True)
    flash_parser.add_argument("--device", required=True)
    flash_parser.add_argument("--image-path", required=True)
    flash_parser.add_argument("--seed-dir", required=True)
    flash_parser.add_argument("--manifest-path", required=True)
    flash_parser.add_argument("--dry-run", action="store_true")
    flash_parser.set_defaults(func=command_flash_image)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - CLI safety net
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
