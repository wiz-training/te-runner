# te-runner: the one image every TE 2.0 lab container/VM references, built only in CI.
# Never bake per-instance state into this shared image: building from scratch each tag keeps that
# class of bug hard to recreate.
# Every download is pinned and verified — against the publisher's checksum file where one exists, a
# literal sha256 where none does (awscli ships only a GPG signature). A mismatch fails the build, so a
# tag rebuilds to the same bytes and a swapped artifact never ships. A bump edits the version here
# (awscli: both hashes, `curl -sSfL <zip> | sha256sum`).
FROM python:3.12-slim-trixie

# openssh-server's postinst mints host keys at install; left in, every grader presents one shared ssh
# identity and anyone holding the image can impersonate a grader. Removed here, minted per boot by
# the entrypoint's `ssh-keygen -A`. No account has a password; the config says so rather than rely on it.
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates git jq unzip less groff openssh-server tmux \
    && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_* \
    && printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\n' > /etc/ssh/sshd_config.d/10-keys-only.conf

# AWS CLI v2 (Amazon names x86_64/aarch64 → uname -m)
RUN ARCH=$(uname -m) && AWSVER=2.37.12 && \
    case "$ARCH" in \
      x86_64)  SHA=e6d3c672fb36ed4b12c354c39a993eee2b34da1437cafb10bac4480840cfa83f ;; \
      aarch64) SHA=829c76096e05f743267d75098bc7cc7cfae799fc7e568554ebaa6d78e6aba753 ;; \
      *) echo "no awscli hash for $ARCH" >&2; exit 1 ;; \
    esac && \
    curl -sSfL "https://awscli.amazonaws.com/awscli-exe-linux-${ARCH}-${AWSVER}.zip" -o /tmp/awscli.zip && \
    echo "$SHA  /tmp/awscli.zip" | sha256sum -c - && \
    unzip -q /tmp/awscli.zip -d /tmp && /tmp/aws/install && rm -rf /tmp/aws /tmp/awscli.zip

# Terraform: labs stage CSP infra from a runner container (the native terraform resource has no
# egress). Floor 1.12: logical operators short-circuit from there, and the Wiz GCP module validates
# `x == null || contains(list, x)` on a null default, which an eager evaluation fails. HashiCorp names
# archives amd64/arm64, so dpkg --print-architecture (not uname -m, which the AWS CLI uses).
RUN ARCH=$(dpkg --print-architecture) && TFVER=1.16.2 && \
    curl -sSfL "https://releases.hashicorp.com/terraform/${TFVER}/terraform_${TFVER}_linux_${ARCH}.zip" -o /tmp/tf.zip && \
    curl -sSfL "https://releases.hashicorp.com/terraform/${TFVER}/terraform_${TFVER}_SHA256SUMS" \
      | grep " terraform_${TFVER}_linux_${ARCH}.zip$" | sed 's#  .*#  /tmp/tf.zip#' | sha256sum -c - && \
    unzip -q /tmp/tf.zip -d /usr/local/bin && rm /tmp/tf.zip

# gcloud + az: native CSP CLIs alongside the AWS one. gcloud from Google's signed apt repo; az via pip
# because Microsoft's apt repo trails new Debian codenames and this image's python is the stable
# interpreter. Entrypoint logs both in from the injected env.
RUN curl -sSf https://packages.cloud.google.com/apt/doc/apt-key.gpg -o /usr/share/keyrings/cloud.google.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
      > /etc/apt/sources.list.d/google-cloud-sdk.list && \
    apt-get update && apt-get install -y --no-install-recommends google-cloud-cli=588.0.0-0 && \
    rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir azure-cli==2.91.0

# Tailscale static binaries (dev-only mesh; entrypoint starts it only when TS_AUTHKEY is injected on
# a dev track). tgz names amd64/arm64 → dpkg --print-architecture.
RUN ARCH=$(dpkg --print-architecture) && TSVER=1.102.3 && \
    curl -sSfL "https://pkgs.tailscale.com/stable/tailscale_${TSVER}_${ARCH}.tgz" -o /tmp/ts.tgz && \
    echo "$(curl -sSfL "https://pkgs.tailscale.com/stable/tailscale_${TSVER}_${ARCH}.tgz.sha256")  /tmp/ts.tgz" | sha256sum -c - && \
    tar -xzf /tmp/ts.tgz -C /tmp && \
    mv "/tmp/tailscale_${TSVER}_${ARCH}/tailscale" "/tmp/tailscale_${TSVER}_${ARCH}/tailscaled" /usr/local/bin/ && \
    rm -rf /tmp/ts.tgz "/tmp/tailscale_${TSVER}_${ARCH}"

# wizcli: the grader runs Wiz Code scans for code-scan labs (publishes the session-tagged CICDScan a
# check grades). Wiz names linux-amd64/arm64, matching dpkg --print-architecture.
RUN ARCH=$(dpkg --print-architecture) && WIZCLI=1.79.0 && \
    curl -sSfL "https://downloads.wiz.io/v1/wizcli/${WIZCLI}/wizcli-linux-${ARCH}" -o /usr/local/bin/wizcli && \
    echo "$(curl -sSfL "https://downloads.wiz.io/v1/wizcli/${WIZCLI}/wizcli-linux-${ARCH}-sha256")  /usr/local/bin/wizcli" | sha256sum -c - && \
    chmod +x /usr/local/bin/wizcli

# kubectl + crane: `k8sconnector ensure` mints the connector's ServiceAccount token with kubectl, and a
# Kubernetes lab's setup seeds a private registry with crane (no container runtime in this image).
# kubectl must sit within one minor of the EKS control plane it talks to (1.31 today; the stable
# channel is too far ahead).
RUN ARCH=$(dpkg --print-architecture) && KVER=v1.31.11 && CRANE=v0.20.6 && \
    curl -sSfL "https://dl.k8s.io/release/${KVER}/bin/linux/${ARCH}/kubectl" -o /usr/local/bin/kubectl && \
    echo "$(curl -sSfL "https://dl.k8s.io/release/${KVER}/bin/linux/${ARCH}/kubectl.sha256")  /usr/local/bin/kubectl" | sha256sum -c - && \
    chmod +x /usr/local/bin/kubectl && \
    CARCH=$([ "$ARCH" = "arm64" ] && echo arm64 || echo x86_64) && \
    curl -sSfL "https://github.com/google/go-containerregistry/releases/download/${CRANE}/go-containerregistry_Linux_${CARCH}.tar.gz" -o /tmp/crane.tgz && \
    curl -sSfL "https://github.com/google/go-containerregistry/releases/download/${CRANE}/checksums.txt" \
      | grep " go-containerregistry_Linux_${CARCH}.tar.gz$" | sed 's#  .*#  /tmp/crane.tgz#' | sha256sum -c - && \
    tar -xzf /tmp/crane.tgz -C /usr/local/bin crane && rm /tmp/crane.tgz

# The image's own identity, passed by CI from the git tag and sha it built. Without it a running
# grader cannot say what it is: a play pins the HCL at import, so a repo's tag string is evidence of
# what main held then, not of what this container runs — and `session verify --min-runner` has nothing
# to compare a lab's floor against. Last, so a rebuild at a new tag reuses every layer above.
ARG TE_RUNNER_TAG=""
ARG TE_RUNNER_REV=""
ENV TE_RUNNER_TAG=${TE_RUNNER_TAG} \
    TE_RUNNER_REV=${TE_RUNNER_REV}
LABEL org.opencontainers.image.version=${TE_RUNNER_TAG} \
      org.opencontainers.image.revision=${TE_RUNNER_REV} \
      org.opencontainers.image.source=https://github.com/wiz-training/te-runner

# The learner shell: a non-root user whose only road to wizlab is a sudoers-scoped wrapper reading a
# 0600 credential snapshot (bin/wizlab-grant writes it from a setup exec's own environment map). The
# container env then carries no operator secret, and a `terminal` opened as `learner` inherits none
# (te-labkit-v2 authoring/instruqt-2.0.md §Secrets and scripts).
RUN apt-get update && apt-get install -y --no-install-recommends sudo && rm -rf /var/lib/apt/lists/* && \
    useradd -m -s /bin/bash learner && \
    printf 'Defaults!/usr/local/bin/wizlab-learner !requiretty\nlearner ALL=(root) NOPASSWD: /usr/local/bin/wizlab-learner\n' \
      > /etc/sudoers.d/learner && chmod 440 /etc/sudoers.d/learner

COPY bin/wizlab bin/wizlab-learner bin/wizlab-grant /usr/local/bin/
COPY wizlab/*.py /usr/local/lib/python3.12/site-packages/wizlab/
COPY reaper/reap_orphans.py /opt/reaper/reap_orphans.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod 755 /usr/local/bin/wizlab /usr/local/bin/wizlab-learner /usr/local/bin/wizlab-grant /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["sleep", "infinity"]
