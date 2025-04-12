#!/bin/bash

set -e

# -----------------------------
# Utility functions
# -----------------------------
log()   { echo -e "\033[1;36m[INFO]\033[0m $1"; }
warn()  { echo -e "\033[1;33m[WARN]\033[0m $1"; }
error() { echo -e "\033[1;31m[ERROR]\033[0m $1"; }

# -----------------------------
# Dynamic image/container name
# -----------------------------
SCRIPT_NAME=$(basename "$0" .sh)

# -----------------------------
# Flags and user args
# -----------------------------
BUILD_IMAGE=false
DOCKER_CMD=""
USER_ARGS=()
GENERATE_KEY=false

# -----------------------------
# Argument parsing
# -----------------------------
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    -b|--build)
      BUILD_IMAGE=true
      shift
      ;;
    --generate-key)
      GENERATE_KEY=true
      shift
      ;;
    --bootstrap)
      DOCKER_CMD="ansible-playbook -i inventory/bootstrap.ini playbooks/01-bootstrap-nodes.yml"
      shift
      ;;
    --prepare)
      DOCKER_CMD="ansible-playbook -i inventory/hosts.ini playbooks/02-prepare-cluster.yml"
      shift
      ;;
    --install)
      DOCKER_CMD="ansible-playbook -i inventory/hosts.ini playbooks/03-install-k8s.yml"
      shift
      ;;
    --init)
      DOCKER_CMD="ansible-playbook -i inventory/hosts.ini playbooks/04-init-cluster.yml"
      shift
      ;;
    --join)
      DOCKER_CMD="ansible-playbook -i inventory/hosts.ini playbooks/05-join-nodes.yml"
      shift
      ;;
    --verify)
      DOCKER_CMD="ansible-playbook -i inventory/hosts.ini playbooks/06-cluster-verify.yml"
      shift
      ;;
    --nfs)
      DOCKER_CMD="ansible-playbook -i inventory/hosts.ini playbooks/07-setup-nfs.yml"
      shift
      ;;
    --verify-nfs)
      DOCKER_CMD="ansible-playbook -i inventory/hosts.ini playbooks/08-verify-nfs.yml"
      shift
      ;;
    --nfs-provisioner)
      DOCKER_CMD="ansible-playbook -i inventory/hosts.ini playbooks/09-setup-nfs-provisioner.yml"
      shift
      ;;
    --metallb)
      DOCKER_CMD="ansible-playbook -i inventory/hosts.ini playbooks/10-setup-metallb.yml"
      shift
      ;;
    --registry)
      DOCKER_CMD="ansible-playbook -i inventory/hosts.ini playbooks/12-setup-registry.yml"
      shift
      ;;
    --shutdown)
      DOCKER_CMD="ansible-playbook -i inventory/hosts.ini playbooks/00-shutdown-nodes.yml"
      shift
      ;;
    --status)
      DOCKER_CMD="ansible-playbook -i inventory/hosts.ini playbooks/00-check-status.yml"
      shift
      ;;
    *)
      USER_ARGS+=("$1")
      shift
      ;;
  esac
done

# -----------------------------
# Dependency checks
# -----------------------------
if ! command -v docker &>/dev/null; then
  error "Docker is not installed or not in your PATH."
  exit 1
fi

if ! docker info &>/dev/null; then
  error "Docker daemon is not running or you don't have permission to access it."
  exit 1
fi

# -----------------------------
# Build image if requested
# -----------------------------
if $BUILD_IMAGE; then
  log "Building Docker image '${SCRIPT_NAME}'..."
  docker build -t "${SCRIPT_NAME}" .
  log "Image built successfully"
fi

# -----------------------------
# Create SSH key if requested
# -----------------------------
if $GENERATE_KEY; then
  mkdir -p config/.ssh
  KEY_PATH="config/.ssh/id_ed25519"
  if [[ -f "$KEY_PATH" ]]; then
    warn "SSH key already exists at $KEY_PATH. Skipping generation."
  else
    log "Generating SSH key pair..."
    ssh-keygen -t ed25519 -C "cluster-control" -f "$KEY_PATH" -N ""
    log "SSH key pair generated."
  fi

  echo ""
  echo "----------------------------------------"
  echo "👉 Copy the following public key to Raspberry Pi Imager:"
  echo ""
  cat "${KEY_PATH}.pub"
  echo "----------------------------------------"
  echo ""
  exit 0
fi

# -----------------------------
# Docker run command
# -----------------------------
DOCKER_ARGS="-it --rm --name ${SCRIPT_NAME}"

if [[ -d "$(pwd)/workdir" ]]; then
  DOCKER_ARGS+=" -v $(pwd)/workdir:/home/ansible/workdir"
fi

if [[ -d "$(pwd)/config/.ssh" ]]; then
  DOCKER_ARGS+=" -v $(pwd)/config/.ssh:/home/ansible/.ssh"
fi

if [[ -d "$(pwd)/config/.kube" ]]; then
  DOCKER_ARGS+=" -v $(pwd)/config/.kube:/home/ansible/.kube"
fi

# -----------------------------
# Run container with correct command
# -----------------------------
if [[ -n "$DOCKER_CMD" ]]; then
  docker run $DOCKER_ARGS "${SCRIPT_NAME}" $DOCKER_CMD
elif [[ "${#USER_ARGS[@]}" -gt 0 ]]; then
  docker run $DOCKER_ARGS "${SCRIPT_NAME}" "${USER_ARGS[@]}"
else
  docker run $DOCKER_ARGS "${SCRIPT_NAME}"
fi
