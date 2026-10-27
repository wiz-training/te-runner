#!/bin/sh
# Stamp container start in UTC so `wizlab correlation inspect --since-boot` grades only state updated
# during this play (`RUN_LAB_DIR` lets `test_entrypoint.py` exercise the write as non-root).
run_lab=${RUN_LAB_DIR:-/run/lab}
if mkdir -p "$run_lab" 2>/dev/null; then
  date -u +%Y-%m-%dT%H:%M:%SZ > "$run_lab/boot_at"
  chmod 0644 "$run_lab/boot_at"
fi

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
# CLI reads AWS_* directly. A secret reaches its CLI through a 0600 file, never argv (readable from
# /proc by any local user while the login runs), and the file goes even when the login fails.
secret_file=""
with_secret() {  # with_secret <value> <command...>: runs the command with the secret's path in $secret_file
  secret_file=$(mktemp /tmp/secret.XXXXXX)
  trap 'rm -f "$secret_file"' EXIT HUP INT TERM
  chmod 600 "$secret_file"
  printf '%s' "$1" > "$secret_file"
  shift
  "$@"
  rm -f "$secret_file"
  trap - EXIT HUP INT TERM
}
gcloud_login() {
  gcloud auth activate-service-account --key-file "$secret_file" --quiet \
    || echo "gcloud service-account login failed" >&2
}
az_login() {  # `@path` expands to the file's contents
  az login --service-principal -u "$ARM_CLIENT_ID" -p "@$secret_file" \
    --tenant "$ARM_TENANT_ID" >/dev/null 2>&1 \
    && az account set --subscription "$ARM_SUBSCRIPTION_ID" \
    || echo "az service-principal login failed" >&2
}
gcloud_auth=
if [ -n "${GOOGLE_CREDENTIALS:-}" ] && command -v gcloud >/dev/null 2>&1; then
  with_secret "$GOOGLE_CREDENTIALS" gcloud_login
  [ -n "${GOOGLE_PROJECT:-}" ] && gcloud config set project "$GOOGLE_PROJECT" --quiet
  gcloud_auth=1
fi
az_auth=
if [ -n "${ARM_CLIENT_ID:-}" ] && command -v az >/dev/null 2>&1; then
  with_secret "$ARM_CLIENT_SECRET" az_login
  az_auth=1
fi
# The learner terminal opens as `learner` (HOME=/home/learner), and neither gcloud nor az reads the
# lease env vars directly: mirror root's CLI config when the corresponding login block ran and populated
# its config directory. `cp -a src/. dst/` after `install -d` avoids nesting on a second run.
root_home=${HOME:-/root}
if [ -n "$gcloud_auth" ] && [ -d "$root_home/.config/gcloud" ]; then
  install -d -m 700 -o learner -g learner /home/learner/.config /home/learner/.config/gcloud
  cp -a "$root_home/.config/gcloud/." /home/learner/.config/gcloud/
  chown -R learner:learner /home/learner/.config/gcloud
fi
if [ -n "$az_auth" ] && [ -d "$root_home/.azure" ]; then
  install -d -m 700 -o learner -g learner /home/learner/.azure
  cp -a "$root_home/.azure/." /home/learner/.azure/
  chown -R learner:learner /home/learner/.azure
fi

exec "$@"
