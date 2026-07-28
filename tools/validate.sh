#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_ROOT
export ANSIBLE_CONFIG="${REPO_ROOT}/workdir/ansible.cfg"
cd "${REPO_ROOT}"

echo "Checking shell scripts..."
bash -n cluster-control.sh tools/validate.sh
shellcheck cluster-control.sh tools/validate.sh

echo "Checking Python..."
python3 -m pip check
ruff check --no-cache tools workdir/tools workdir/inventory tests
ruff format --check --no-cache tools workdir/tools workdir/inventory tests
python3 -m unittest discover -s tests -p 'test_*.py'

echo "Checking YAML and Ansible..."
yamllint .
ansible-lint

for playbook in workdir/playbooks/*.yml; do
  inventory="workdir/inventory/hosts.ini"
  case "${playbook}" in
    *01-bootstrap* | *18-recover-worker* | *19-render-recovery* | \
      *21-recover-control* | *22-render-master* | *24-await*)
      inventory="workdir/inventory/bootstrap.ini"
      ;;
  esac

  ansible-playbook --inventory "${inventory}" --syntax-check "${playbook}" >/dev/null
done

echo "All repository validation checks passed."
