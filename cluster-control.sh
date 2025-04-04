#!/bin/bash

set -e

# -----------------------------
# Utility functions
# -----------------------------
log()   { echo -e "\033[1;36m[INFO]\033[0m $1"; }
warn()  { echo -e "\033[1;33m[WARN]\033[0m $1"; }
error() { echo -e "\033[1;31m[ERROR]\033[0m $1"; }

# -----------------------------
# Flags
# -----------------------------
BUILD_IMAGE=false

# -----------------------------
# Argument parsing
# -----------------------------
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    -b|--build)
      BUILD_IMAGE=true
      shift
      ;;
    *)
      warn "Unknown parameter passed: $1"
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

log "Docker is available ✅"

# -----------------------------
# Build image if requested
# -----------------------------
if $BUILD_IMAGE; then
  log "Building Docker image 'cluster-control'..."
  docker build -t cluster-control .
  log "Image built successfully ✅"
fi

# -----------------------------
# Docker run command
# -----------------------------
DOCKER_ARGS="-it --rm"
DOCKER_ARGS+=" --name cluster-control"

# Mount project folders if they exist
if [[ -d "$(pwd)/ansible" ]]; then
  log "Mounting ./ansible into container"
  DOCKER_ARGS+=" -v $(pwd)/ansible:/home/ansible/ansible"
fi

if [[ -d "$(pwd)/config" ]]; then
  log "Mounting ./config into container"
  DOCKER_ARGS+=" -v $(pwd)/config:/home/ansible/config"
fi

# -----------------------------
# Run container
# -----------------------------
log "Launching cluster-control container..."
docker run $DOCKER_ARGS cluster-control
