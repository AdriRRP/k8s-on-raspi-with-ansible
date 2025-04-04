# ⚙️ Ansible RPi K8s CCA - Cluster Control & Automation

This project provides a complete environment to deploy, maintain, and monitor a real Kubernetes cluster running on Raspberry Pi 4 devices. It leverages modern tools such as Ansible, Docker, Helm, K9s, and more.

> Everything you need to build a real Kubernetes cluster on RPi using declarative and automated infrastructure as code.

---

## 📦 Features

- 🎛️ Manage your cluster from a Docker container with `ansible`, `kubectl`, `helm`, `k9s`, etc.
- 🤖 Ansible-driven automation (playbooks, inventory, bootstrap, upgrades)
- 📦 Optional private Docker registry
- 🔍 Declarative infrastructure management for nodes and services
- 🔐 Automatic etcd backup support
- 🔄 Prepared for remote maintenance and in-place upgrades with no downtime

---

## 🧰 Requirements

### Host (your local computer)
- macOS or Linux
- [Docker](https://docs.docker.com/get-docker/) installed and running

### Raspberry Pi Cluster
- 4x Raspberry Pi 4 (8GB recommended, but any model should work)
- 4x microSD cards (32GB minimum, flashed with Ubuntu Server 24.04 64-bit)
- Local network (Ethernet switch recommended)
- USB flash drives or SSD (optional for NFS or persistent volumes)

> 📥 [Download Raspberry Pi Imager](https://www.raspberrypi.com/software/)
>
> 📘 [Official guide: Flashing OS to SD card](https://www.raspberrypi.com/documentation/computers/getting-started.html)

---

## 🚀 Getting Started

### 1. Clone the repository

```bash
git clone https://github.com/your-username/ansible-rpi-k8s-cca.git
cd ansible-rpi-k8s-cca
```

### 2. Launch the control environment

```bash
./cluster-control.sh --build
```

This will build the Docker image and launch a container with all the tools required to manage the cluster.

### 3. Run commands from inside the container

```bash
ansible-inventory -i ansible/inventory.ini --list
ansible-playbook ansible/playbooks/setup.yml
kubectl get nodes
k9s
```

---

## 📁 Project Structure

```
├── ansible/               # Playbooks and roles to configure the cluster
│   ├── inventory.ini      # Inventory file with RPi IPs
│   ├── playbooks/         # Playbooks organized by function
│   └── roles/             # Reusable roles
├── config/                # SSH keys and cluster kubeconfig
├── cluster-control.sh     # Script to launch the control container
├── Dockerfile             # Image with all required tools
└── README.md              # This document
```

---

## ✅ TODO / Roadmap

- [ ] Add OS provisioning support (e.g. via Imager or netboot)
- [ ] Implement automated etcd and volume backups
- [ ] Add Ansible playbook for rolling Kubernetes upgrades
- [ ] Integrate cluster monitoring (Prometheus, Grafana, etc.)
- [ ] Centralized logging stack (Loki, Fluentbit...)

---

## 📚 Useful Resources

- [Kubernetes Documentation](https://kubernetes.io/docs/)
- [Ansible Documentation](https://docs.ansible.com/)
- [Raspberry Pi Imager](https://www.raspberrypi.com/software/)

---

> Made with ❤️ and frustration by [AdriRRP](https://github.com/AdriRRP) 🐧🛠️

