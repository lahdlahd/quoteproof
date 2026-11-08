#!/usr/bin/env bash
# STEP 3 - gVisor (runsc) from the official gVisor apt repository, 'release' channel.
#   sudo bash deploy/03_install_gvisor.sh
# Starts the 90-minute gVisor timebox clock (checked by step 4).
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }
export DEBIAN_FRONTEND=noninteractive

[[ -f /root/qp-evidence/gvisor-timebox-start ]] || date -u +%s > /root/qp-evidence/gvisor-timebox-start
echo "gVisor timebox started at $(date -u -d @"$(cat /root/qp-evidence/gvisor-timebox-start)")"

curl -fsSL https://gvisor.dev/archive.key | gpg --dearmor --yes -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
  > /etc/apt/sources.list.d/gvisor.list
apt-get update
apt-get install -y runsc
runsc --version

# Registers the 'runsc' runtime in /etc/docker/daemon.json (default platform: systrap,
# which needs no nested virtualisation). We do NOT make it Docker's default runtime;
# sandboxd selects it explicitly on every run.
runsc install
systemctl restart docker
echo "== /etc/docker/daemon.json"; cat /etc/docker/daemon.json
echo "STEP 3 DONE - now run: sudo bash deploy/04_gvisor_proof.sh"
