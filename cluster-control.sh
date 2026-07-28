#!/bin/bash

set -euo pipefail

log()   { printf '\033[1;36m[INFO]\033[0m %s\n' "$1"; }
warn()  { printf '\033[1;33m[WARN]\033[0m %s\n' "$1"; }
error() { printf '\033[1;31m[ERROR]\033[0m %s\n' "$1" >&2; }

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
SCRIPT_NAME="$(basename "$0" .sh)"
readonly SCRIPT_NAME
readonly IMAGE_NAME="${SCRIPT_NAME}"
readonly REPO_ROOT="${SCRIPT_DIR}"

cd "${REPO_ROOT}"

build_image=false
generate_key=false
validate_repo=false
docker_cmd=""
host_cmd=""
operation_count=0
user_args=()
docker_env_args=()
playbook_extra_args=()
docker_build_args=()
media_node=""
media_device=""
media_bundle_id=""
media_image_url=""
media_sha256sums_url=""
media_refresh=false
media_dry_run=false
upgrade_execution_mode_option=""
upgrade_scope_option=""

usage() {
  cat <<'EOF'
Usage:
  ./cluster-control.sh [operation] [options] [-- ansible-arguments]

Core lifecycle:
  --build                         Build the pinned control image
  --generate-key                  Generate config/.ssh/id_ed25519
  --bootstrap                     Bootstrap users and SSH access
  --prepare                       Configure and tune all nodes
  --install                       Install containerd and Kubernetes
  --init                          Initialize the control plane
  --join                          Join worker nodes
  --verify                        Run cluster smoke tests

Platform services:
  --nfs | --verify-nfs | --nfs-provisioner
  --metallb | --registry | --monitoring

Performance:
  --baseline                      Capture a read-only cluster baseline
  --benchmark                     Run bounded, ephemeral active benchmarks
  --performance-profile           Apply one reversible tuning profile
  --node-local-dns                Audit or explicitly manage NodeLocal DNS

Maintenance:
  --status | --shutdown
  --reconcile-node-hygiene
  --discover-cluster | --upgrade-plan | --upgrade-cluster
  --upgrade-latest-stable | --post-upgrade-reconcile
  --repair-apt-sources | --configure-k8s-repo

Recovery (experimental for the control plane):
  --capture-recovery-bundle | --rehearse-master-recovery
  --render-recovery-seeds | --render-master-recovery-seed
  --prepare-node-media | --flash-node-media | --list-removable-disks
  --await-recovery-node | --recover-worker | --recover-master | --verify-recovery

Quality:
  --validate                      Run the complete local pre-commit gate

Discovery options:
  --discovery-strategy MODE --discovery-cidr CIDR
  --discovery-ssh-user USER --discovery-ssh-port PORT
  --discovery-ssh-key PATH --discovery-static-inventory PATH

Upgrade options:
  --target-version VERSION        Kubernetes target, for example 1.36.3
  --target-os-version VERSION     Ubuntu target, for example 26.04
  --target-deb-revision REVISION
  --dry-run | --apply-upgrade | --cluster-window
  --os-only | --kubernetes-only
  --os-patch-nodes | --no-os-patch-nodes
  --os-release-nodes | --no-os-release-nodes
  --enforce-replicas | --allow-single-replica | --allow-no-pdb
  --disable-snapshots

Recovery and media options:
  --node NAME --device /dev/diskN --recovery-bundle-id ID
  --image-url URL --sha256sums-url URL
  --refresh-media | --media-dry-run

General:
  --limit HOST                    Passed through to ansible-playbook
  -h, --help

Use "--" before additional raw Ansible arguments. Exactly one operation may be
selected; --build can be combined with a container-backed operation.
EOF
}

require_option_value() {
  local option="$1"
  local value="${2-}"

  if [[ -z "${value}" || "${value}" == --* ]]; then
    error "${option} requires a value."
    exit 2
  fi
}

mark_operation() {
  operation_count=$((operation_count + 1))
}

shell_join() {
  printf '%q ' "$@"
}

catalog_kubectl_version() {
  local catalog_path="${PWD}/workdir/inventory/group_vars/all.yml"

  if [[ ! -f "${catalog_path}" ]]; then
    return 1
  fi

  awk '
    $1 == "kubernetes:" { in_kubernetes = 1; next }
    in_kubernetes && /^[^[:space:]]/ { in_kubernetes = 0 }
    in_kubernetes && $1 == "install_version:" {
      gsub(/"/, "", $2)
      print "v" $2
      exit
    }
  ' "${catalog_path}"
}

catalog_os_value() {
  local wanted_key="$1"
  local catalog_path="${PWD}/workdir/inventory/group_vars/all.yml"

  if [[ ! -f "${catalog_path}" ]]; then
    return 1
  fi

  awk -v wanted_key="${wanted_key}" '
    $1 == "platform_release_catalog:" { in_catalog = 1; next }
    in_catalog && $1 == "os:" { in_os = 1; next }
    in_os && /^[^[:space:]]/ { in_os = 0 }
    in_os && $1 == wanted_key ":" {
      gsub(/"/, "", $2)
      print $2
      exit
    }
  ' "${catalog_path}"
}

inventory_node_group() {
  local node_name="$1"
  local inventory_path="${PWD}/workdir/inventory/bootstrap.ini"

  if [[ ! -f "${inventory_path}" ]]; then
    return 1
  fi

  awk -v node_name="${node_name}" '
    /^\[/ {
      current_group = $0
      gsub(/[\[\]]/, "", current_group)
      next
    }
    $1 == node_name {
      print current_group
      exit
    }
  ' "${inventory_path}"
}

latest_local_recovery_bundle_id() {
  local bundles_root="${PWD}/config/.kube/outputs/recovery-bundles"

  if [[ ! -d "${bundles_root}" ]]; then
    return 1
  fi

  find "${bundles_root}" -mindepth 1 -maxdepth 1 -type d | sort | tail -n 1 | xargs basename
}

default_node_media_image_url() {
  local codename image_filename
  codename="$(catalog_os_value download_codename)"
  image_filename="$(catalog_os_value preinstalled_server_image_filename)"

  if [[ -z "${codename}" || -z "${image_filename}" ]]; then
    return 1
  fi

  printf 'https://cdimage.ubuntu.com/ubuntu/releases/%s/release/%s\n' "${codename}" "${image_filename}"
}

default_node_media_sha256sums_url() {
  local codename
  codename="$(catalog_os_value download_codename)"

  if [[ -z "${codename}" ]]; then
    return 1
  fi

  printf 'https://cdimage.ubuntu.com/ubuntu/releases/%s/release/SHA256SUMS\n' "${codename}"
}

run_host_command() {
  if ! command -v python3 >/dev/null 2>&1; then
    error "python3 is required on the host for node media automation."
    exit 1
  fi

  python3 "${REPO_ROOT}/tools/node_media.py" "$@"
}

prepare_node_media() {
  local node_group seed_dir manifest_path image_url sha256sums_url bundle_id_resolved

  if [[ -z "${media_node}" ]]; then
    error "--node is required for --prepare-node-media"
    exit 1
  fi

  node_group="$(inventory_node_group "${media_node}" || true)"
  if [[ -z "${node_group}" ]]; then
    error "Node '${media_node}' was not found in workdir/inventory/bootstrap.ini."
    exit 1
  fi

  if [[ "${node_group}" == "master" ]]; then
    "${REPO_ROOT}/cluster-control.sh" --render-master-recovery-seed
    bundle_id_resolved="${media_bundle_id:-$(latest_local_recovery_bundle_id || true)}"
  else
    "${REPO_ROOT}/cluster-control.sh" --render-recovery-seeds --limit "${media_node}"
    bundle_id_resolved=""
  fi

  image_url="${media_image_url:-$(default_node_media_image_url || true)}"
  sha256sums_url="${media_sha256sums_url:-$(default_node_media_sha256sums_url || true)}"
  seed_dir="${REPO_ROOT}/config/.kube/outputs/recovery-seeds/${media_node}"
  manifest_path="${REPO_ROOT}/config/.kube/outputs/node-media/${media_node}/manifest.json"

  if [[ -z "${image_url}" || -z "${sha256sums_url}" ]]; then
    error "Could not resolve the default Ubuntu image URLs from the release catalog."
    exit 1
  fi

  mkdir -p "${REPO_ROOT}/config/.cache/node-media" "${REPO_ROOT}/config/.kube/outputs/node-media/${media_node}"

  if $media_refresh; then
    run_host_command prepare-image \
      --node "${media_node}" \
      --seed-dir "${seed_dir}" \
      --image-url "${image_url}" \
      --sha256sums-url "${sha256sums_url}" \
      --image-cache-dir "${REPO_ROOT}/config/.cache/node-media" \
      --manifest-path "${manifest_path}" \
      --bundle-id "${bundle_id_resolved}" \
      --refresh
  else
    run_host_command prepare-image \
      --node "${media_node}" \
      --seed-dir "${seed_dir}" \
      --image-url "${image_url}" \
      --sha256sums-url "${sha256sums_url}" \
      --image-cache-dir "${REPO_ROOT}/config/.cache/node-media" \
      --manifest-path "${manifest_path}" \
      --bundle-id "${bundle_id_resolved}"
  fi
}

flash_node_media() {
  local manifest_path image_path seed_dir

  if [[ -z "${media_node}" ]]; then
    error "--node is required for --flash-node-media"
    exit 1
  fi

  if [[ -z "${media_device}" ]]; then
    error "--device is required for --flash-node-media"
    exit 1
  fi

  manifest_path="${REPO_ROOT}/config/.kube/outputs/node-media/${media_node}/manifest.json"
  if [[ ! -f "${manifest_path}" ]]; then
    warn "Prepared node-media manifest not found for ${media_node}; preparing it now."
    prepare_node_media
  fi

  image_path="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["image_path"])' "${manifest_path}")"
  seed_dir="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["seed_dir"])' "${manifest_path}")"

  if $media_dry_run; then
    run_host_command flash-image \
      --node "${media_node}" \
      --device "${media_device}" \
      --image-path "${image_path}" \
      --seed-dir "${seed_dir}" \
      --manifest-path "${manifest_path}" \
      --dry-run
  else
    run_host_command flash-image \
      --node "${media_node}" \
      --device "${media_device}" \
      --image-path "${image_path}" \
      --seed-dir "${seed_dir}" \
      --manifest-path "${manifest_path}"
  fi
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      while [[ "$#" -gt 0 ]]; do
        user_args+=("$1")
        shift
      done
      break
      ;;
    --build)
      build_image=true
      ;;
    --generate-key)
      mark_operation
      generate_key=true
      ;;
    --validate)
      mark_operation
      validate_repo=true
      ;;
    --list-removable-disks)
      mark_operation
      host_cmd="list-removable-disks"
      ;;
    --prepare-node-media)
      mark_operation
      host_cmd="prepare-node-media"
      ;;
    --flash-node-media)
      mark_operation
      host_cmd="flash-node-media"
      ;;
    --bootstrap)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/bootstrap.ini playbooks/01-bootstrap-nodes.yml"
      ;;
    --prepare)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/hosts.ini playbooks/02-prepare-cluster.yml"
      ;;
    --install)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/hosts.ini playbooks/03-install-k8s.yml"
      ;;
    --init)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/hosts.ini playbooks/04-init-cluster.yml"
      ;;
    --join)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/hosts.ini playbooks/05-join-nodes.yml"
      ;;
    --verify)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/hosts.ini playbooks/06-cluster-verify.yml"
      ;;
    --nfs)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/hosts.ini playbooks/07-setup-nfs.yml"
      ;;
    --verify-nfs)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/hosts.ini playbooks/08-verify-nfs.yml"
      ;;
    --nfs-provisioner)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/hosts.ini playbooks/09-setup-nfs-provisioner.yml"
      ;;
    --metallb)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/hosts.ini playbooks/10-setup-metallb.yml"
      ;;
    --registry)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/hosts.ini playbooks/12-setup-registry.yml"
      ;;
    --monitoring)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/hosts.ini playbooks/13-setup-monitoring.yml"
      ;;
    --baseline)
      mark_operation
      baseline_repository_revision="$(git rev-parse HEAD 2>/dev/null || printf 'unknown')"
      docker_cmd="ansible-playbook -i inventory/hosts.ini playbooks/25-capture-performance-baseline.yml -e performance_baseline_repository_revision=${baseline_repository_revision}"
      ;;
    --benchmark)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/hosts.ini playbooks/27-run-performance-benchmark.yml"
      ;;
    --performance-profile)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/hosts.ini playbooks/28-apply-performance-profile.yml"
      ;;
    --node-local-dns)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/hosts.ini playbooks/29-manage-node-local-dns.yml"
      ;;
    --reconcile-node-hygiene)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/hosts.ini playbooks/26-reconcile-node-hygiene.yml"
      ;;
    --discover-cluster)
      mark_operation
      docker_cmd="python3 inventory/discover_cluster.py --list --write /home/ansible/.kube/outputs/discovered_inventory.json"
      ;;
    --upgrade-plan)
      mark_operation
      docker_cmd="DISCOVERY_WRITE_PATH=/home/ansible/.kube/outputs/discovered_inventory.json ansible-playbook -i inventory/discover_cluster.py playbooks/14-upgrade-plan.yml"
      ;;
    --upgrade-cluster)
      mark_operation
      docker_cmd="DISCOVERY_WRITE_PATH=/home/ansible/.kube/outputs/discovered_inventory.json ansible-playbook -i inventory/discover_cluster.py playbooks/15-upgrade-cluster.yml"
      ;;
    --upgrade-latest-stable)
      mark_operation
      docker_cmd="DISCOVERY_WRITE_PATH=/home/ansible/.kube/outputs/discovered_inventory.json python3 tools/upgrade_to_latest_stable.py"
      ;;
    --post-upgrade-reconcile)
      mark_operation
      docker_cmd="DISCOVERY_WRITE_PATH=/home/ansible/.kube/outputs/discovered_inventory.json ansible-playbook -i inventory/discover_cluster.py playbooks/16-post-upgrade-reconcile.yml"
      ;;
    --capture-recovery-bundle)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/hosts.ini playbooks/17-capture-recovery-bundle.yml"
      ;;
    --rehearse-master-recovery)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/hosts.ini playbooks/20-rehearse-control-plane-recovery.yml"
      ;;
    --recover-master)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/bootstrap.ini playbooks/21-recover-control-plane-node.yml"
      ;;
    --recover-worker)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/bootstrap.ini playbooks/18-recover-worker-node.yml"
      ;;
    --render-recovery-seeds)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/bootstrap.ini playbooks/19-render-recovery-seeds.yml"
      ;;
    --render-master-recovery-seed)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/bootstrap.ini playbooks/22-render-master-recovery-seed.yml"
      ;;
    --await-recovery-node)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/bootstrap.ini playbooks/24-await-recovery-node.yml"
      ;;
    --verify-recovery)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/hosts.ini playbooks/23-verify-recovered-cluster.yml"
      ;;
    --shutdown)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/hosts.ini playbooks/00-shutdown-nodes.yml"
      ;;
    --status)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/hosts.ini playbooks/00-check-status.yml"
      ;;
    --repair-apt-sources)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/hosts.ini playbooks/00-repair-apt-sources.yml"
      ;;
    --configure-k8s-repo)
      mark_operation
      docker_cmd="ansible-playbook -i inventory/hosts.ini playbooks/00-configure-k8s-repo.yml"
      ;;
    --discovery-strategy)
      require_option_value "$1" "${2-}"
      docker_env_args+=(-e "DISCOVERY_STRATEGY=$2")
      shift
      ;;
    --discovery-cidr)
      require_option_value "$1" "${2-}"
      docker_env_args+=(-e "DISCOVERY_SCAN_CIDR=$2")
      shift
      ;;
    --discovery-ssh-user)
      require_option_value "$1" "${2-}"
      docker_env_args+=(-e "DISCOVERY_SSH_USER=$2")
      shift
      ;;
    --discovery-ssh-port)
      require_option_value "$1" "${2-}"
      docker_env_args+=(-e "DISCOVERY_SSH_PORT=$2")
      shift
      ;;
    --discovery-ssh-key)
      require_option_value "$1" "${2-}"
      docker_env_args+=(-e "DISCOVERY_SSH_KEY=$2")
      shift
      ;;
    --discovery-static-inventory)
      require_option_value "$1" "${2-}"
      docker_env_args+=(-e "DISCOVERY_STATIC_INVENTORY=$2")
      shift
      ;;
    --recovery-bundle-id)
      require_option_value "$1" "${2-}"
      playbook_extra_args+=(-e "control_plane_restore_bundle_id=$2")
      media_bundle_id="$2"
      shift
      ;;
    --node)
      require_option_value "$1" "${2-}"
      media_node="$2"
      shift
      ;;
    --device)
      require_option_value "$1" "${2-}"
      media_device="$2"
      shift
      ;;
    --image-url)
      require_option_value "$1" "${2-}"
      media_image_url="$2"
      shift
      ;;
    --sha256sums-url)
      require_option_value "$1" "${2-}"
      media_sha256sums_url="$2"
      shift
      ;;
    --refresh-media)
      media_refresh=true
      ;;
    --media-dry-run)
      media_dry_run=true
      ;;
    --target-version)
      require_option_value "$1" "${2-}"
      playbook_extra_args+=(-e "upgrade_target_kubernetes_version=$2")
      shift
      ;;
    --target-os-version)
      require_option_value "$1" "${2-}"
      playbook_extra_args+=(-e "upgrade_target_os_version=$2")
      shift
      ;;
    --target-deb-revision)
      require_option_value "$1" "${2-}"
      playbook_extra_args+=(-e "upgrade_target_kubernetes_deb_revision=$2")
      shift
      ;;
    --dry-run)
      if [[ -n "${upgrade_execution_mode_option}" && "${upgrade_execution_mode_option}" != "dry-run" ]]; then
        error "--dry-run and --apply-upgrade are mutually exclusive."
        exit 2
      fi
      upgrade_execution_mode_option="dry-run"
      playbook_extra_args+=(-e "upgrade_execution_mode=dry-run")
      ;;
    --apply-upgrade)
      if [[ -n "${upgrade_execution_mode_option}" && "${upgrade_execution_mode_option}" != "apply" ]]; then
        error "--dry-run and --apply-upgrade are mutually exclusive."
        exit 2
      fi
      upgrade_execution_mode_option="apply"
      playbook_extra_args+=(-e "upgrade_execution_mode=apply")
      ;;
    --os-only)
      if [[ -n "${upgrade_scope_option}" && "${upgrade_scope_option}" != "os" ]]; then
        error "Select only one upgrade scope."
        exit 2
      fi
      upgrade_scope_option="os"
      playbook_extra_args+=(-e "upgrade_maintenance_scope=os")
      playbook_extra_args+=(-e "upgrade_os_patch_nodes=true")
      ;;
    --kubernetes-only)
      if [[ -n "${upgrade_scope_option}" && "${upgrade_scope_option}" != "kubernetes" ]]; then
        error "Select only one upgrade scope."
        exit 2
      fi
      upgrade_scope_option="kubernetes"
      playbook_extra_args+=(-e "upgrade_maintenance_scope=kubernetes")
      ;;
    --cluster-window)
      if [[ -n "${upgrade_scope_option}" && "${upgrade_scope_option}" != "cluster" ]]; then
        error "Select only one upgrade scope."
        exit 2
      fi
      upgrade_scope_option="cluster"
      playbook_extra_args+=(-e "upgrade_maintenance_scope=cluster")
      ;;
    --os-patch-nodes)
      playbook_extra_args+=(-e "upgrade_os_patch_nodes=true")
      ;;
    --os-release-nodes)
      playbook_extra_args+=(-e "upgrade_os_release_nodes=true")
      ;;
    --no-os-release-nodes)
      playbook_extra_args+=(-e "upgrade_os_release_nodes=false")
      ;;
    --no-os-patch-nodes)
      playbook_extra_args+=(-e "upgrade_os_patch_nodes=false")
      ;;
    --enforce-replicas)
      playbook_extra_args+=(-e "upgrade_enforce_replicated_workloads=true")
      ;;
    --allow-single-replica)
      playbook_extra_args+=(-e "upgrade_enforce_replicated_workloads=false")
      ;;
    --allow-no-pdb)
      playbook_extra_args+=(-e "upgrade_require_pdb_protection=false")
      ;;
    --disable-snapshots)
      playbook_extra_args+=(-e "upgrade_capture_snapshots=false")
      ;;
    *)
      user_args+=("$1")
      ;;
  esac
  shift
done

if (( operation_count > 1 )); then
  error "Select exactly one operation per invocation; --build may be combined with it."
  exit 2
fi

if (( operation_count == 0 )) && {
  [[ "${#docker_env_args[@]}" -gt 0 ]] ||
    [[ "${#playbook_extra_args[@]}" -gt 0 ]] ||
    [[ -n "${media_node}${media_device}${media_bundle_id}${media_image_url}${media_sha256sums_url}" ]] ||
    $media_refresh ||
    $media_dry_run
}; then
  error "Options were provided without an operation."
  exit 2
fi

if $build_image && { [[ -n "${host_cmd}" ]] || $generate_key; }; then
  error "--build cannot be combined with a host-only media or SSH-key operation."
  exit 2
fi

if $generate_key; then
  mkdir -p config/.ssh
  key_path="config/.ssh/id_ed25519"
  if [[ -f "${key_path}" ]]; then
    warn "SSH key already exists at ${key_path}. Skipping generation."
  else
    log "Generating SSH key pair..."
    ssh-keygen -t ed25519 -C "cluster-control" -f "${key_path}" -N ""
    log "SSH key pair generated."
  fi

  echo ""
  echo "----------------------------------------"
  echo "Copy the following public key to Raspberry Pi Imager:"
  echo ""
  cat "${key_path}.pub"
  echo "----------------------------------------"
  echo ""
  exit 0
fi

if [[ -n "${host_cmd}" ]]; then
  case "${host_cmd}" in
    list-removable-disks)
      run_host_command list-disks
      ;;
    prepare-node-media)
      prepare_node_media
      ;;
    flash-node-media)
      flash_node_media
      ;;
    *)
      error "Unknown host command: ${host_cmd}"
      exit 1
      ;;
  esac
  exit 0
fi

if ! command -v docker >/dev/null 2>&1; then
  error "Docker is not installed or not in your PATH."
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  error "Docker daemon is not running or you don't have permission to access it."
  exit 1
fi

if $build_image; then
  kubectl_build_version="${KUBECTL_VERSION:-}"

  if [[ -z "${kubectl_build_version}" ]]; then
    kubectl_build_version="$(catalog_kubectl_version || true)"
  fi

  if [[ -n "${kubectl_build_version}" ]]; then
    docker_build_args+=(--build-arg "KUBECTL_VERSION=${kubectl_build_version}")
    log "Using kubectl ${kubectl_build_version} for the control image build."
  fi

  log "Building Docker image '${IMAGE_NAME}'..."
  if [[ "${#docker_build_args[@]}" -gt 0 ]]; then
    docker build "${docker_build_args[@]}" -t "${IMAGE_NAME}" .
  else
    docker build -t "${IMAGE_NAME}" .
  fi
  log "Image built successfully"
fi

if $validate_repo; then
  docker run --rm \
    --security-opt no-new-privileges \
    --volume "${REPO_ROOT}:/src:ro" \
    --workdir /src \
    "${IMAGE_NAME}" \
    ./tools/validate.sh
  exit 0
fi

if $build_image && (( operation_count == 0 )) && [[ "${#user_args[@]}" -eq 0 ]]; then
  exit 0
fi

mkdir -p config/.kube/outputs

docker_args=(
  --rm
  --security-opt
  no-new-privileges
)

if [[ -t 0 && -t 1 ]]; then
  docker_args=(-it "${docker_args[@]}")
fi

if [[ "${#docker_env_args[@]}" -gt 0 ]]; then
  docker_args+=("${docker_env_args[@]}")
fi

if [[ -d "${REPO_ROOT}/workdir" ]]; then
  docker_args+=(-v "${REPO_ROOT}/workdir:/home/ansible/workdir")
fi

if [[ -d "${REPO_ROOT}/config/.ssh" ]]; then
  docker_args+=(-v "${REPO_ROOT}/config/.ssh:/home/ansible/.ssh")
fi

if [[ -d "${REPO_ROOT}/config/.kube" ]]; then
  docker_args+=(-v "${REPO_ROOT}/config/.kube:/home/ansible/.kube")
fi

quoted_extra_args=""
if [[ "${#playbook_extra_args[@]}" -gt 0 ]]; then
  quoted_extra_args+="$(shell_join "${playbook_extra_args[@]}")"
fi

if [[ "${#user_args[@]}" -gt 0 ]]; then
  quoted_extra_args+="$(shell_join "${user_args[@]}")"
fi

if [[ -n "${docker_cmd}" ]]; then
  docker run "${docker_args[@]}" "${IMAGE_NAME}" /bin/bash -lc "${docker_cmd} ${quoted_extra_args}"
elif [[ "${#user_args[@]}" -gt 0 ]]; then
  docker run "${docker_args[@]}" "${IMAGE_NAME}" "${user_args[@]}"
else
  docker run "${docker_args[@]}" "${IMAGE_NAME}"
fi
