# Raspberry Pi Kubernetes with Ansible

An opinionated, containerized Ansible project for building and maintaining a
small `kubeadm` Kubernetes cluster on Raspberry Pi nodes running Ubuntu Server
ARM64.

It automates:

- host bootstrap, OS tuning and flash-wear reduction
- containerd and Kubernetes installation
- control-plane initialization and worker enrollment
- rolling Ubuntu and Kubernetes upgrades
- NFS storage, MetalLB, an internal registry and lightweight monitoring
- cluster health checks, snapshots and recovery artifacts

The default topology is one control-plane node and three workers. It is intended
for a homelab or edge lab, not as a highly available production platform.

## Safety model

- All control tooling runs in a pinned Docker image.
- Runtime credentials and generated outputs stay under `config/`, outside Git
  and outside the Docker build context.
- Kubernetes and Ubuntu upgrades default to `dry-run`; mutation requires
  `--apply-upgrade`.
- Nodes are upgraded serially, with preflight checks, drain/uncordon, snapshots
  and post-upgrade verification.
- Registry and monitoring services default to `ClusterIP`.
- Kubernetes workloads have resource limits, health probes and conservative
  security contexts.
- Control-plane restore exists as experimental code, but its end-to-end
  fire-drill is not yet accepted. Do not rely on it as the only backup strategy.

## Requirements

Control host:

- macOS or Linux with Docker
- network access to every node over SSH
- `python3`, `xz` and `sudo` only for the optional macOS media-flashing flow

Nodes:

- Raspberry Pi 4 or newer with 64-bit Ubuntu Server
- stable wired networking and unique hostnames
- DHCP reservations or static addresses matching the inventories
- SSH access for the bootstrap user

Review these files before the first run:

- `workdir/inventory/bootstrap.ini`: initial Ubuntu user and node addresses
- `workdir/inventory/hosts.ini`: managed admin user and node addresses
- `workdir/inventory/group_vars/all.yml`: versions and cluster policy
- `workdir/roles/nfs_server/defaults/main.yml`: NFS disk and export settings
- `workdir/roles/metallb/defaults/main.yml`: address pool

The sample inventories use `192.168.0.100-103`; adapt them to your LAN.

## Quick start

Build the control image:

```bash
./cluster-control.sh --build
```

Generate the control SSH key:

```bash
./cluster-control.sh --generate-key
```

Add `config/.ssh/id_ed25519.pub` to the initial Ubuntu user on each node, then
run the lifecycle in order:

```bash
./cluster-control.sh --bootstrap
./cluster-control.sh --prepare
./cluster-control.sh --install
./cluster-control.sh --init
./cluster-control.sh --join
./cluster-control.sh --verify
```

Each stage is independently rerunnable. Limit a safe operation to one host with
an Ansible argument:

```bash
./cluster-control.sh --status --limit raspi-worker1
```

Run `./cluster-control.sh --help` for the complete command overview.

## Platform services

Deploy optional services after the base cluster is healthy:

```bash
./cluster-control.sh --nfs
./cluster-control.sh --verify-nfs
./cluster-control.sh --nfs-provisioner
./cluster-control.sh --metallb
./cluster-control.sh --registry
./cluster-control.sh --monitoring
```

Important defaults:

| Component | Default |
| --- | --- |
| NFS device | `/dev/sda1`; creation requires enablement and exact device confirmation |
| NFS export | `/mnt/nfs-ssd` |
| StorageClass | `raspi-nfs-provisioner` |
| MetalLB pool | `192.168.0.240-192.168.0.250` |
| Pod network | `10.244.0.0/16`, must not overlap the node LAN |
| Service network | `10.96.0.0/12`, must not overlap node or pod networks |
| Registry | persistent, internal `ClusterIP` |
| Prometheus | 7-day retention, 10 GiB PVC |
| Metrics Server | native CPU/memory Metrics API for Lens, `kubectl top` and HPA |
| Grafana | internal `ClusterIP`, generated admin password |

Pod and Service CIDRs are immutable installation decisions. The defaults above
apply to new clusters; existing clusters keep their live values and must not be
silently migrated.

Generated passwords, kubeconfig files, snapshots and reports are written below
`config/.kube/outputs/`.

Access internal services without exposing them to the LAN:

```bash
./cluster-control.sh kubectl --kubeconfig /home/ansible/.kube/config \
  --namespace monitoring port-forward service/grafana 3000:3000
```

The Grafana password is stored in
`config/.kube/outputs/grafana-admin-password`.

### Lens metrics

The monitoring playbook deploys two complementary sources: Prometheus for
historical monitoring and Metrics Server for Kubernetes' current CPU/memory
resource API. In Lens, open the cluster settings with `Cmd+Shift+T`, select
`Metrics`, set `Metrics Source` to `Prometheus`, then change the Prometheus query
format from `Auto Detect Prometheus` to `Helm`. This reveals the service address
field; enter:

```text
monitoring/prometheus:9090
```

Click outside the field to persist it. Do not enable the bundled stack under
`Lens Metrics`, because this repository already manages Prometheus,
node-exporter and kube-state-metrics. The Prometheus role validates the metric
names and node labels used by Lens before completing.

Metrics Server uses the official aggregated API and is deliberately not treated
as a historical monitoring backend. Verify both paths with:

```bash
kubectl --kubeconfig config/.kube/config top nodes
kubectl --kubeconfig config/.kube/config get --raw /apis/metrics.k8s.io/v1beta1/nodes
```

Kubeadm's default kubelet serving certificates are self-signed, so the
Metrics Server connection to kubelets defaults to
`metrics_server_kubelet_insecure_tls: true`. This is limited to the trusted
cluster network and can be disabled after introducing an audited kubelet
serving-certificate signing and approval workflow.

## Version policy

`platform_release_catalog` in
`workdir/inventory/group_vars/all.yml` is the single source of truth for:

- Ubuntu install and supported release-upgrade hops
- Kubernetes versions and Debian package revisions
- containerd, runc and CNI plugins
- Calico, MetalLB, monitoring, registry and validation images

Fresh installs use the version validated by the repository. Existing clusters
move through every supported Ubuntu release and every Kubernetes minor; minor
versions are never skipped.

The control image's default `kubectl` version mirrors the catalog and may be
overridden for a staged operation:

```bash
KUBECTL_VERSION=v1.36.3 ./cluster-control.sh --build
```

## Upgrades

Discovery tries the existing kubeconfig first and falls back to the static
inventory. A bounded CIDR scan is available when explicitly requested:

```bash
./cluster-control.sh --discover-cluster
./cluster-control.sh --discover-cluster \
  --discovery-strategy scan \
  --discovery-cidr 192.168.0.0/24
```

Always create and inspect a plan first:

```bash
./cluster-control.sh --upgrade-plan \
  --target-version 1.36.3 \
  --target-os-version 26.04
```

The automatic route also defaults to a non-mutating plan:

```bash
./cluster-control.sh --upgrade-latest-stable --dry-run
```

Apply only after reviewing the generated plan and snapshot:

```bash
./cluster-control.sh --upgrade-latest-stable --apply-upgrade
```

For separate maintenance windows:

```bash
./cluster-control.sh --upgrade-cluster \
  --os-only \
  --os-patch-nodes \
  --apply-upgrade

./cluster-control.sh --upgrade-cluster \
  --kubernetes-only \
  --target-version 1.36.3 \
  --apply-upgrade
```

The preflight rejects unsupported version skips, unhealthy nodes, unsafe
single-replica workloads and missing PodDisruptionBudgets unless an explicit
override is supplied. Overrides such as `--allow-single-replica` and
`--allow-no-pdb` reduce availability guarantees and should be exceptional.
Drain also refuses unmanaged Pods by default; use
`-- -e upgrade_force_drain=true` only after inspecting them. Ephemeral
`emptyDir` data is deleted during an accepted drain.

## Performance baseline

Capture a non-mutating point-in-time baseline:

```bash
./cluster-control.sh --baseline
```

The `observe` profile reads the Kubernetes API and each node over SSH. It does
not install packages, deploy workloads or change node configuration. Every run
creates `cluster.json`, one JSON document per node, `summary.json` and a concise
`report.md` below:

```text
config/.kube/outputs/benchmarks/YYYYMMDDTHHMMSSZ/
```

Captured signals include API readiness latency, node and Pod health, CPU load
and frequency policy, memory availability, Pressure Stall Information,
temperature, Raspberry Pi throttling flags when `vcgencmd` is available, disk
usage and counters, runtime storage, network counters, failed units, cumulative
Pod restarts and drift from the catalogued Kubernetes version.

Run the baseline several times under comparable idle conditions before treating
a difference as a regression. Later load-generating CPU, network and storage
profiles must remain explicit opt-ins with bounded writes and automatic cleanup.

Run the complementary active-safe control benchmark explicitly:

```bash
./cluster-control.sh --benchmark
```

It measures persistent API latency, Pod startup, DNS and Pod-to-Pod throughput
inside a temporary restricted namespace. Sample counts, CPU/memory and network
traffic are bounded, no storage load is generated, and cleanup runs even after
an error. Results are written below
`config/.kube/outputs/benchmarks-active/YYYYMMDDTHHMMSSZ/`.

Name an A/B experiment without changing the safety limits:

```bash
./cluster-control.sh --benchmark \
  -- -e performance_benchmark_experiment=schedutil
```

Apply one experiment at a time, then rerun the benchmark:

```bash
./cluster-control.sh --performance-profile \
  -- -e performance_tuning_profile=schedutil
./cluster-control.sh --benchmark \
  -- -e performance_benchmark_experiment=schedutil
```

The supported profiles are `schedutil` and `parallel-pulls`. Application is
serial and waits for every node to return `Ready`. Restore both defaults with:

```bash
./cluster-control.sh --performance-profile \
  -- -e performance_tuning_profile=control
```

Treat `parallel-pulls` as an experimental capability, not a recommended default.
The active-safe benchmark deliberately reuses cached images and does not write a
cold image set to flash, so it cannot prove a benefit from concurrent pulls.

NodeLocal DNS is separate because it changes the DNS data path. Its default is
read-only `audit`; enabling and rollback both require an explicit state:

```bash
./cluster-control.sh --node-local-dns
./cluster-control.sh --node-local-dns -- -e node_local_dns_state=enabled
./cluster-control.sh --benchmark \
  -- -e performance_benchmark_experiment=node-local-dns
./cluster-control.sh --node-local-dns -- -e node_local_dns_state=absent
```

The role supports only the documented kube-proxy `iptables` path, uses a pinned
multi-architecture image with ARM64 support, waits for one cache Pod per node,
verifies cluster DNS after both enable and rollback, and automatically removes
the cache resources when enablement verification fails.

## Raspberry Pi policy

The default node policy favors stability and flash lifetime:

- volatile, bounded journald storage
- APT autoclean, autoremove and cache cleanup
- stale temporary-file cleanup
- `fstrim.timer` where discard is supported
- swap disabled
- persisted Kubernetes networking modules and sysctls
- no broad custom `iptables` accepts; active UFW trusts only `cluster_node_subnet`
- stale persisted Kubernetes/Calico rule snapshots are detected and disabled
- known duplicate cloud-init logrotate transitions are repaired only on checksum match
- kubelet system reservations, image garbage collection and log rotation
- serial package and node upgrades
- optional admin utilities disabled unless `common_install_admin_tools` is enabled

Reconcile these housekeeping protections without running the full node
preparation lifecycle:

```bash
./cluster-control.sh --reconcile-node-hygiene
```

Networking defaults to kube-proxy `iptables` and Calico `Iptables`. The nftables
path is opt-in and both components must be changed together:

```yaml
kube_proxy_mode: "nftables"
calico_linux_dataplane: "Nftables"
```

## Recovery status

Worker re-provisioning and recovery artifact generation are available, but
control-plane replacement remains experimental.

Useful non-destructive preparation commands:

```bash
./cluster-control.sh --capture-recovery-bundle
./cluster-control.sh --rehearse-master-recovery
./cluster-control.sh --render-recovery-seeds
```

The macOS media helper verifies Ubuntu's published checksum and rejects disks
that are internal, virtual, non-removable or not whole-disk devices:

```bash
./cluster-control.sh --list-removable-disks
./cluster-control.sh --prepare-node-media --node raspi-worker1
./cluster-control.sh --flash-node-media \
  --node raspi-worker1 \
  --device /dev/diskN \
  --media-dry-run
```

Remove `--media-dry-run` only after checking the exact device. Flashing destroys
all data on that device.

Current control-plane recovery limitations:

- a single control-plane node is an unavoidable API and etcd single point of
  failure
- the automated restore has not passed a complete replacement-media fire-drill
- there is no automated serial-console capture for early boot failures

A future accepted recovery design should include repeated physical fire-drills
and preferably a three-node HA control plane or external replicated etcd.

## Validation

Run the same pre-commit gate as GitHub Actions:

```bash
./cluster-control.sh --validate
```

It runs:

- Bash syntax and ShellCheck
- Ruff lint and format checks
- Python unit tests
- `yamllint`
- `ansible-lint` with the production profile
- `ansible-playbook --syntax-check` for every playbook

Build and validate against a clean toolchain in one invocation:

```bash
./cluster-control.sh --build --validate
```

The CI workflow uses read-only permissions, immutable action SHAs and the same
pinned control image. Dependabot checks Actions, Docker and Python dependencies
weekly; platform catalog updates still require an explicit validation change.

## Repository layout

```text
.
├── cluster-control.sh              # user-facing entrypoint
├── config/                         # ignored runtime secrets and outputs
├── tests/                          # unit and policy tests
├── tools/                          # host-side media and validation tools
└── workdir/
    ├── inventory/                  # static and dynamic inventory
    ├── playbooks/                  # lifecycle entrypoints
    ├── roles/                      # reusable Ansible implementation
    ├── tools/                      # upgrade planning and reporting
    └── requirements*.{txt,yml}     # pinned control dependencies
```

## Operational limits

- This is a single-control-plane design, not HA.
- NFS hosted on that node is also a storage single point of failure.
- The project does not back up application-level data inside persistent
  volumes.
- Passwordless sudo is enabled for the managed admin user by default. Disable
  `admin_passwordless_sudo` only after providing another non-interactive Ansible
  privilege-escalation strategy.
- `--shutdown` targets every inventory node and is intentionally disruptive.

## Future work

| Priority | Improvement | Acceptance criterion |
| --- | --- | --- |
| P0 | Accept control-plane recovery and move to three control-plane nodes or replicated external etcd | Three consecutive replacement-media fire-drills restore a healthy cluster without using the original system disk |
| P0 | Protect persistent application data and etcd independently of the cluster | Scheduled, encrypted backups pass automated restore tests on disposable storage |
| P1 | Remove the NFS single point of failure | Storage survives loss of one node or disk and workloads recover within a documented objective |
| P1 | Migrate clusters created with overlapping Pod and LAN CIDRs | Preflight reports overlap and a rehearsed rebuild or migration runbook preserves required data |
| P1 | Pin workload images by multi-architecture digest and hash Python artifacts | CI verifies digests, hashes, SBOM generation and supported ARM64 manifests |
| P2 | Remove the temporary `var-naming` lint exclusion | Shared variables are namespaced and the production lint profile passes without skips |
| P2 | Replace SSH trust-on-first-use and passwordless sudo defaults | Host keys are provisioned from a trusted source and unattended privilege escalation uses a scoped secret |
| P2 | Add scheduled hardware integration testing | A dedicated Raspberry Pi environment validates bootstrap, upgrade, reboot and rollback paths |
| P2 | Choose and add an explicit project license | Repository redistribution terms are documented in a root `LICENSE` file |

Upstream references:

- [kubeadm cluster upgrades](https://kubernetes.io/docs/tasks/administer-cluster/kubeadm/kubeadm-upgrade/)
- [Kubernetes version skew policy](https://kubernetes.io/releases/version-skew-policy/)
- [Ansible roles](https://docs.ansible.com/projects/ansible-core/devel/playbook_guide/playbooks_reuse_roles.html)
- [Ubuntu Server on Raspberry Pi](https://documentation.ubuntu.com/hardware-support/boards/how-to/ubuntu_supported/raspberry-pi/)
