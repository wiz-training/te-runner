#!/bin/sh
# Dev-only remote access. When a real tailnet key is injected (dev tracks only), join the tailnet and
# open a key-based sshd so an operator can reach this grader for live iteration. The gate is the key's
# shape, not its presence: the team store rejects an empty value, so a lab that keeps its dev-access
# block live holds a placeholder between leases, and `lease ensure` overwrites it with a `tskey-`.
# Never `set -e`: the grader must reach its keepalive even if dev access fails to come up.
case "${TS_AUTHKEY:-}" in tskey-*) dev_access=1 ;; *) dev_access= ;; esac
if [ -n "$dev_access" ]; then
  # No TUN device in a container → userspace networking. Ephemeral state: a fresh node per boot is
  # correct for a throwaway dev lease. Tailscale SSH is deliberately NOT used — it needs a TUN and
  # hangs in userspace mode; we run a real sshd and reach it over the tailnet IP.
  mkdir -p /var/run/tailscale
  tailscaled --tun=userspace-networking --state=mem: --socks5-server=localhost:1055 \
    >/var/log/tailscaled.log 2>&1 &
  i=0
  while [ ! -S /var/run/tailscale/tailscaled.sock ] && [ "$i" -lt 10 ]; do i=$((i + 1)); sleep 1; done
  tailscale up --authkey "$TS_AUTHKEY" --hostname "${TS_HOSTNAME:-grader-dev}" \
    || echo "tailscale up failed (see /var/log/tailscaled.log)" >&2

  # Same shape gate: a placeholder must not become an authorized_keys line.
  case "${TE_DEV_SSH_PUBKEY:-}" in ssh-*|ecdsa-*) pubkey=1 ;; *) pubkey= ;; esac
  if [ -n "$pubkey" ]; then
    install -d -m 700 /root/.ssh
    printf '%s\n' "$TE_DEV_SSH_PUBKEY" > /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
    ssh-keygen -A
    mkdir -p /run/sshd
    /usr/sbin/sshd
  fi

  # Persistent session that carries THIS process's injected env (WIZ_*/AWS/lease creds); a plain sshd
  # session starts with a clean env, so attach here instead: `tmux attach -t dev`.
  tmux new-session -d -s dev
fi

# Log the native CLIs in from the lease env (terraform-standard names) so gcloud/az work like the
# AWS CLI does. Best-effort: a CLI login failing must not stop the grader. AWS needs nothing — the
# CLI reads AWS_* directly.
if [ -n "${GOOGLE_CREDENTIALS:-}" ] && command -v gcloud >/dev/null 2>&1; then
  gcp_key=$(mktemp /tmp/gcp-sa.XXXXXX)
  trap 'rm -f "$gcp_key"' EXIT HUP INT TERM
  chmod 600 "$gcp_key"
  printf '%s' "$GOOGLE_CREDENTIALS" > "$gcp_key"
  gcloud auth activate-service-account --key-file "$gcp_key" --quiet \
    || echo "gcloud service-account login failed" >&2
  rm -f "$gcp_key"
  trap - EXIT HUP INT TERM
  [ -n "${GOOGLE_PROJECT:-}" ] && gcloud config set project "$GOOGLE_PROJECT" --quiet
fi
if [ -n "${ARM_CLIENT_ID:-}" ] && command -v az >/dev/null 2>&1; then
  az login --service-principal -u "$ARM_CLIENT_ID" -p "$ARM_CLIENT_SECRET" \
    --tenant "$ARM_TENANT_ID" >/dev/null 2>&1 \
    && az account set --subscription "$ARM_SUBSCRIPTION_ID" \
    || echo "az service-principal login failed" >&2
fi

exec "$@"
