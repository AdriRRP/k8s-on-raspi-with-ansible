
# Table of Contents

1. [Introduction](#introduction)
2. [Prerequisites](#prerequisites)
3. [Project Structure](#project-structure)
4. [Cluster Setup Workflow](#cluster-setup-workflow)
   - [Control Environment](#control-environment)
   - [SSH Key Generation](#ssh-key-generation)
   - [Flashing Raspberry Pi OS](#flashing-raspberry-pi-os)
   - [Static IP Assignment](#static-ip-assignment)
   - [Inventory Setup](#inventory-setup)
   - [Cluster Bootstrapping](#cluster-bootstrapping)
   - [Node Preparation](#node-preparation)
   - [Kubernetes Installation](#kubernetes-installation)
   - [Control Plane Initialization](#control-plane-initialization)
   - [Worker Node Join](#worker-node-join)
   - [Cluster Verification](#cluster-verification)
   - [NFS Storage Setup](#nfs-storage-setup)
   - [Load Balancer Support with MetalLB](#load-balancer-support-with-metallb)
   - [Internal Container Registry](#internal-container-registry)
   - [Monitoring Stack](#monitoring-stack)


---

## Introduction

This project provides a robust, declarative, and fully automated framework for deploying and maintaining a Kubernetes cluster using Raspberry Pi 4 (8GB) boards. It is designed to run on-premise with a minimal yet powerful control environment that includes Ansible, kubectl, helm, k9s, and other tools. Everything is orchestrated via a control script and organized into modular Ansible roles and playbooks.

## Prerequisites

### Hardware
- 4x Raspberry Pi 4 (8GB recommended)
- 4x microSD cards **or** USB flash drives (32GB minimum)
- Ethernet switch and cables
- Optional USB SSDs for persistent volumes

### Host System (Control Machine)
- Docker installed and running (Linux or macOS)

> Note: All provisioning and configuration tasks are executed inside a Docker container; the host OS only needs Docker.

## Project Structure

```
.
├── cluster-control.sh        # Master script for all operations
├── config/                   # SSH keys and kubeconfigs
├── Dockerfile                # Control environment image
├── README.md                 # This file
└── workdir/
    ├── ansible.cfg
    ├── inventory/
    │   ├── bootstrap.ini     # Inventory using initial OS user (e.g., ubuntu)
    │   └── hosts.ini         # Inventory using admin user (e.g., admin)
    ├── playbooks/            # Ansible playbooks
    └── roles/                # Modular Ansible roles
```

## Cluster Setup Workflow

Everything is orchestrated through the `cluster-control.sh` script. You should use it exclusively to interact with the cluster environment.

### Control Environment

This phase:
- Builds a Docker image with all required tools: Ansible, kubectl, helm, k9s, jq, etc.
- Prepares an isolated control environment for cluster management

Run:

```bash
./cluster-control.sh --build
```

### SSH Key Generation

This phase:
- Generates an SSH key pair (ed25519) inside `config/.ssh/`
- Prepares the public key for inclusion in the OS image flashing process

Run:

```bash
./cluster-control.sh --generate-key
```

**Why ed25519?**
- Stronger security with smaller key size
- Faster connection handshakes
- Recommended as default in modern OpenSSH (since v7.0)

### Flashing Raspberry Pi OS

This phase:
- Flashes each Raspberry Pi with a compatible OS (tested with Ubuntu Server 24.10)
- Applies initial configuration: hostname, SSH key, locale, and timezone

Use Raspberry Pi Imager with advanced settings:
- Set hostname (e.g., `raspi-master`, `raspi-worker1`, etc.)
- Set user/password (e.g., `ubuntu`)
- Enable SSH with the key from `config/.ssh/id_ed25519.pub`
- Set locale and timezone

Repeat this for each Raspberry Pi device.

> Make sure all devices use Ethernet, not Wi-Fi.

### Static IP Assignment

This phase:
- Ensures each Raspberry Pi node has a fixed IP address
- Guarantees predictable inventory and connectivity

Use your router's DHCP reservation or static config to assign:

- `raspi-master` → `192.168.0.100`
- `raspi-worker1` → `192.168.0.101`
- `raspi-worker2` → `192.168.0.102`
- `raspi-worker3` → `192.168.0.103`

Reboot the devices after setting IPs.

### Inventory Setup

This phase:
- Defines which nodes are part of the cluster and how to access them
- Starts with the default OS user, then transitions to the `admin` user

Step 1: Create `bootstrap.ini`:

```ini
[master]
raspi-master ansible_host=192.168.0.100 ansible_user=ubuntu

[workers]
raspi-worker1 ansible_host=192.168.0.101 ansible_user=ubuntu
raspi-worker2 ansible_host=192.168.0.102 ansible_user=ubuntu
raspi-worker3 ansible_host=192.168.0.103 ansible_user=ubuntu

[cluster:children]
master
workers
```

Step 2: Create a copy named `hosts.ini`, replacing the initial user with `admin`. This will be the process's main inventory file:

```bash
sed 's/ansible_user=ubuntu/ansible_user=admin/' inventory/bootstrap.ini > inventory/hosts.ini
```

### Cluster Bootstrapping

This phase:
- Adds node host keys to `known_hosts` for SSH trust
- Installs base tools
- Creates an `admin` user with sudo privileges and SSH-only access

Run:

```bash
./cluster-control.sh --bootstrap
```

### Node Preparation

Run with the updated inventory using the admin user:

```bash
./cluster-control.sh --prepare
```

This phase:
- Disables swap
- Updates the system
- Sets hostnames and `/etc/hosts`
- Configures SSH key exchange among nodes
- Ensures required kernel params and firewall rules

### Kubernetes Installation

This phase:
- Installs containerd with appropriate configuration for Kubernetes
- Downloads and installs `kubeadm`, `kubelet`, and `kubectl`
- Enables and starts the `kubelet` service

Run:

```bash
./cluster-control.sh --install
```

### Control Plane Initialization

Initialize the control plane and install Calico:

```bash
./cluster-control.sh --init
```

This phase:
- Runs `kubeadm init`
- Fetches `admin.conf` and stores it as `config/.kube/config`
- Installs Tigera Operator and Calico CRDs
- Removes control-plane taint to allow scheduling

### Worker Node Join

This phase:
- Copies the `kubeadm_join_cmd.sh` script generated during init to each worker node
- Executes the script to join each node to the control plane
- Cleans up the temporary script after successful join

Run:

```bash
./cluster-control.sh --join
```

### Cluster Verification

This phase:
- Waits until all nodes are in `Ready` state
- Launches a temporary pod to test basic scheduling and networking
- Deploys a DaemonSet to verify pod-to-pod connectivity between nodes
- Checks CoreDNS availability and service resolution using a test pod

Run:

```bash
./cluster-control.sh --verify
```

---

## NFS Storage Setup

To enable dynamic Persistent Volume provisioning via NFS, this project supports setting up an NFS server on the master node with a USB-attached SSD.

### Step 1: Install and Configure NFS Server

Ensure the SSD is connected to `raspi-master` and identified (e.g., `/dev/sda1`). Then run:

```bash
./cluster-control.sh --nfs
```

This will:
- Optionally format the disk (if `nfs_format_device: true`)
- Mount it to `/mnt/nfs-ssd` with fstab persistence
- Export it via `nfs-kernel-server` to all cluster nodes

> ⚠️ You can control whether the disk is formatted using the variable `nfs_format_device` in `roles/nfs-server/defaults/main.yml`.

### Step 2: Ensure NFS Client Tools are Installed

The `common` role includes `nfs-common` to allow all nodes to mount NFS shares. If needed, you can reapply the common setup with:

```bash
./cluster-control.sh --prepare
```

This guarantees `nfs-common` is installed across the cluster.

### Step 3: Verify NFS is Reachable from All Nodes

Run the following to perform a read/write test from each node:

```bash
./cluster-control.sh --verify-nfs
```

This creates a temporary mount, writes a test file, reads it back, and cleans up — ensuring that all nodes can access the NFS share correctly.

### Step 4: Deploy the NFS Provisioner

To enable dynamic PVC provisioning via Kubernetes, deploy the external provisioner:

```bash
./cluster-control.sh --nfs-provisioner
```

This will:
- Deploy the `nfs-subdir-external-provisioner` as a Deployment
- Create the necessary RBAC rules
- Register a `StorageClass` named `raspi-nfs-provisioner`

The provisioner will mount the exported NFS volume (`/mnt/nfs-ssd`) and create subdirectories automatically for each PVC.

### Step 5: Verify Dynamic PVC Provisioning

After deploying the provisioner, the setup automatically:
- Creates a temporary PVC using the `raspi-nfs-provisioner` class
- Attaches it to a busybox Pod
- Writes a test file inside the volume
- Verifies the content
- Cleans up both PVC and Pod

You can also run this verification step again anytime:

```bash
./cluster-control.sh --nfs-provisioner
```

This ensures the entire storage pipeline — from NFS server to automatic PVCs — is functioning end-to-end.

---

## Load Balancer Support with MetalLB

To enable support for external `LoadBalancer` services within your on-premise Raspberry Pi Kubernetes cluster, this project includes a declarative setup for [MetalLB](https://metallb.universe.tf/).

### Step 1: Deploy MetalLB

MetalLB will be installed in native mode with custom IP address pool and L2 advertisement. Simply run:

```bash
./cluster-control.sh --metallb
```

This will:

- Apply the official MetalLB manifests
- Wait for all MetalLB pods to become ready (`controller`, `speaker`, `webhook`)
- Create an `IPAddressPool` with your configured address range (default: `192.168.0.240-192.168.0.250`)
- Create a `L2Advertisement` to announce services at L2 level (ARP)
- Verify MetalLB is operating correctly with a full echo-service test:
  - A Pod running [`ealen/echo-server`](https://hub.docker.com/r/ealen/echo-server) is deployed
  - A LoadBalancer service is exposed using MetalLB
  - The host attempts a direct HTTP request to the external IP
  - The response is validated and all resources are cleaned up

### Customization

You can change the IP range or namespace by editing:
```yaml
roles/metallb/defaults/main.yml
```

Example:
```yaml
metallb_address_pool:
  name: default
  addresses:
    - 192.168.0.240-192.168.0.250
```

Ensure that the selected IP range is not used by your router's DHCP pool and is routable within your LAN.

---

## Internal Container Registry

This project includes a self-hosted container image registry that runs inside the Kubernetes cluster and is accessible both internally and (optionally) externally via MetalLB.

### Step 1: Deploy the Registry

Run:

```bash
./cluster-control.sh --registry
```

This will:

- Create a Deployment in the `kube-system` namespace using the official `registry:2` image
- Expose it as a `ClusterIP` or `LoadBalancer` service (depending on configuration)
- Optionally assign a static IP via MetalLB (e.g. `192.168.0.250`)
- Perform a test from within the cluster using a BusyBox pod to ensure internal reachability
- Perform a test from the control host using `curl` to ensure external access (if MetalLB is enabled)

### Accessing the Registry

#### From within the cluster
Use the internal DNS name:
```
http://registry.kube-system.svc.cluster.local:5000
```

#### From your LAN (via MetalLB)
If `registry_expose_lb: true` and `registry_lb_ip` are defined:
```
http://192.168.0.250:5000
```
Test externally with:
```bash
curl http://192.168.0.250:5000/v2/
```
Expected output:
```
{}
```

### Customization

You can configure the registry settings in:
```yaml
roles/registry/defaults/main.yml
```
Example:
```yaml
registry_port: 5000
registry_namespace: kube-system
registry_expose_lb: true
registry_lb_ip: 192.168.0.250
```

If you want persistence, modify the Deployment to use a PersistentVolumeClaim instead of `emptyDir`.

### Security Note
- By default, this registry is deployed **without authentication or TLS**.
- It is intended for internal development use only.
- If you plan to expose it beyond your LAN, secure it with a reverse proxy or use TLS + basic auth.

---

## Monitoring Stack

This project includes a lightweight monitoring stack based on Prometheus, Grafana, and exporters such as `node-exporter` and `kube-state-metrics`. The stack is fully integrated with Kubernetes and managed via Ansible.

### Deployment

To deploy the monitoring stack:

```bash
./cluster-control.sh --monitoring
```

This will:

- Create the `monitoring` namespace (if missing).
- Deploy Prometheus with:
  - Kubernetes service discovery.
  - Persistent volume (PVC).
  - MetalLB LoadBalancer service.
- Deploy `node-exporter` as a DaemonSet.
- Deploy `kube-state-metrics`.
- Deploy Grafana with:
  - Prometheus as default datasource.
  - Persistent storage.
  - API-based dashboard provisioning.

### Accessing Grafana

Grafana will be exposed at:

```
http://<grafana-loadbalancer-ip>:3000
```

Default credentials:

- **Username:** `admin`
- **Password:** `admin` (or as defined in `defaults/main.yml`)

### Adding Dashboards by ID

To customize which dashboards are imported into Grafana, edit this list in:

```
roles/grafana/defaults/main.yml
```

```yaml
grafana_dashboards:
  - id: 1860
    name: node-exporter-full
  - id: 13332
    name: kube-state-metrics
```

You can find more dashboards at [grafana.com/dashboards](https://grafana.com/grafana/dashboards/).

### Updating Dashboards

To re-import dashboards:

```bash
./cluster-control.sh --monitoring
```

This ensures dashboards are re-fetched and re-imported via Grafana’s API.

### Notes

- Dashboards are validated (must have `uid` and `title`).
- `${DS_PROMETHEUS}` placeholders are auto-replaced with the correct datasource.
- Uses the Grafana HTTP API — no need to mount dashboard JSON files.

> This monitoring setup is optimized for Raspberry Pi clusters: low memory footprint and declarative configuration.
