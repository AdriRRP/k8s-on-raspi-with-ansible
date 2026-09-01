FROM ubuntu:26.04@sha256:2260313b31c8c011cd2eebe728008efac1b3982be73eb71348ea2648d2c0e09b

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PATH=/opt/ansible-venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ARG KUBECTL_VERSION=v1.36.3
ARG ARCH_NAME

COPY workdir/requirements.yml /tmp/ansible-requirements.yml
COPY workdir/requirements-control.txt /tmp/requirements-control.txt
COPY workdir/requirements-control-lock.txt /tmp/requirements-control-lock.txt

RUN if [ -z "$ARCH_NAME" ]; then \
      ARCH_RAW="$(uname -m)" && \
      case "$ARCH_RAW" in \
        x86_64) echo "ARCH_DL=amd64" > /tmp/arch_env ;; \
        aarch64) echo "ARCH_DL=arm64" > /tmp/arch_env ;; \
        *) echo "Unsupported architecture: $ARCH_RAW" && exit 1 ;; \
      esac; \
    else \
      case "$ARCH_NAME" in \
        x86_64) echo "ARCH_DL=amd64" > /tmp/arch_env ;; \
        aarch64) echo "ARCH_DL=arm64" > /tmp/arch_env ;; \
        *) echo "Unsupported architecture override: $ARCH_NAME" && exit 1 ;; \
      esac; \
    fi

RUN apt-get update && apt-get install -y --no-install-recommends \
    apache2-utils \
    bash \
    ca-certificates \
    curl \
    git \
    openssh-client \
    openssl \
    python3 \
    python3-pip \
    python3-venv \
    shellcheck \
    && rm -rf /var/lib/apt/lists/*

RUN . /tmp/arch_env && \
    curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH_DL}/kubectl" && \
    curl -fsSLo /tmp/kubectl.sha256 "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH_DL}/kubectl.sha256" && \
    echo "$(cat /tmp/kubectl.sha256)  /usr/local/bin/kubectl" | sha256sum --check --strict && \
    rm -f /tmp/kubectl.sha256 /tmp/arch_env && \
    chmod 0755 /usr/local/bin/kubectl

RUN python3 -m venv /opt/ansible-venv && \
    pip install --no-cache-dir --requirement /tmp/requirements-control-lock.txt && \
    mkdir -p /usr/share/ansible/collections && \
    ansible-galaxy collection install \
      --collections-path /usr/share/ansible/collections \
      --requirements-file /tmp/ansible-requirements.yml && \
    rm -f \
      /tmp/ansible-requirements.yml \
      /tmp/requirements-control.txt \
      /tmp/requirements-control-lock.txt

RUN useradd --create-home --shell /bin/bash ansible

USER ansible
WORKDIR /home/ansible/workdir
SHELL ["/bin/bash", "-lc"]
