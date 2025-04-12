FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# ---- BUILD ARGUMENTS ----
ARG KUBECTL_VERSION=latest
ARG ARCH_NAME

# ---- ARCHITECTURE DETECTION ----
RUN if [ -z "$ARCH_NAME" ]; then \
      ARCH_RAW=$(uname -m) && \
      case "$ARCH_RAW" in \
        x86_64) echo "ARCH_NAME=x86_64" > /tmp/arch_env && echo "ARCH_DL=amd64" >> /tmp/arch_env ;; \
        aarch64) echo "ARCH_NAME=aarch64" > /tmp/arch_env && echo "ARCH_DL=arm64" >> /tmp/arch_env ;; \
        *) echo "Unsupported architecture: $ARCH_RAW" && exit 1 ;; \
      esac; \
    else \
      echo "ARCH_NAME=$ARCH_NAME" > /tmp/arch_env && \
      case "$ARCH_NAME" in \
        x86_64) echo "ARCH_DL=amd64" >> /tmp/arch_env ;; \
        aarch64) echo "ARCH_DL=arm64" >> /tmp/arch_env ;; \
        *) echo "Unsupported architecture override: $ARCH_NAME" && exit 1 ;; \
      esac; \
    fi

# ---- BASE TOOLS INSTALL ----
RUN apt-get update && apt-get install -y \
    curl ca-certificates gnupg lsb-release sudo git \
    ssh openssh-client python3 python3-pip ansible \
    wget unzip bash-completion software-properties-common \
    && rm -rf /var/lib/apt/lists/*

# ---- KUBECTL INSTALL ----
RUN . /tmp/arch_env && \
    if [ "$KUBECTL_VERSION" = "latest" ]; then \
      KUBECTL_VERSION=$(curl -L -s https://dl.k8s.io/release/stable.txt); \
    fi && \
    curl -LO "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH_DL}/kubectl" && \
    install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl && \
    rm kubectl

# ---- HELM INSTALL ----
RUN curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# ---- K9S INSTALL (.deb) ----
RUN . /tmp/arch_env && \
    K9S_VERSION=$(curl -s https://api.github.com/repos/derailed/k9s/releases/latest | grep tag_name | cut -d '"' -f 4) && \
    DEB_NAME="k9s_linux_${ARCH_DL}.deb" && \
    wget -O "/tmp/${DEB_NAME}" "https://github.com/derailed/k9s/releases/download/${K9S_VERSION}/${DEB_NAME}" && \
    apt-get update && apt-get install -y "/tmp/${DEB_NAME}" && \
    rm "/tmp/${DEB_NAME}" && \
    rm -rf /var/lib/apt/lists/*

# ---- CREATE DEFAULT USER ----
RUN useradd -ms /bin/zsh ansible && echo "ansible ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# ---- LOCALE SETUP FOR UTF-8 SUPPORT ----
RUN apt-get update && apt-get install -y locales && \
    locale-gen en_US.UTF-8
ENV LANG=en_US.UTF-8 \
    LANGUAGE=en_US:en \
    LC_ALL=en_US.UTF-8

# ---- SHELL & DEVELOPMENT TOOLS ----
RUN apt-get update && apt-get install -y \
    zsh vim tree fzf htop jq yq fonts-powerline python3-kubernetes \
    && rm -rf /var/lib/apt/lists/*

# ---- INSTALL OH-MY-ZSH AND CONFIGURE THEME/PLUGINS ----
USER ansible
WORKDIR /home/ansible/workdir
RUN RUNZSH=no CHSH=no KEEP_ZSHRC=yes \
    sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" && \
    sed -i 's/^ZSH_THEME=.*/ZSH_THEME="agnoster"/' ~/.zshrc && \
    sed -i 's/^plugins=.*/plugins=(git kubectl ansible fzf)/' ~/.zshrc && \
    echo 'source <(kubectl completion zsh)' >> ~/.zshrc && \
    echo 'source <(helm completion zsh)' >> ~/.zshrc && \
    echo 'autoload -U +X compinit && compinit' >> ~/.zshrc

# ---- DEFAULT SHELL ----
SHELL ["/bin/zsh", "-c"]
