#!/usr/bin/env bash
# STEP 1 - base packages, Linux users/groups, folders, firewall.
# Run on the VM from the unpacked repo folder:   sudo bash deploy/01_host_setup.sh
# Safe to re-run (idempotent). Does NOT install Docker or gVisor.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }
. /etc/os-release
if [[ "${ID}" != "ubuntu" || "${VERSION_ID}" != "24.04" ]]; then
  echo "STOP: Ubuntu 24.04 is required, found: ${PRETTY_NAME}"; exit 1
fi
export DEBIAN_FRONTEND=noninteractive

echo "== 1/4 packages"
apt-get update
apt-get -y upgrade
apt-get install -y ca-certificates curl gnupg ufw python3 python3-venv python3-pytest jq unzip rsync

echo "== 2/4 groups and service users (no login shells)"
for g in qp-files qp-sock; do getent group "$g" >/dev/null || groupadd --system "$g"; done
mkuser() {
  id "$1" &>/dev/null || useradd --system --home-dir "/var/lib/$1" --create-home --shell /usr/sbin/nologin "$1"
  chmod 0700 "/var/lib/$1"
}
mkuser qp-web
mkuser qp-worker
mkuser qp-sandbox
usermod -aG qp-files qp-web             # can write uploads
usermod -aG qp-sock qp-worker           # can talk to the supervisor socket
usermod -aG qp-files,qp-sock qp-sandbox # can read uploads, owns the socket (docker group added in step 2)

echo "== 3/4 folders"
install -d -o root -g root -m 0755 /srv/qp /opt/qp /opt/qp/app
install -d -o qp-web -g qp-files -m 2750 /srv/qp/uploads      # setgid: new files get group qp-files
install -d -o qp-sandbox -g qp-sandbox -m 0711 /srv/qp/staging
install -d -o root -g qp-sandbox -m 0750 /etc/qp
install -d -o root -g root -m 0700 /root/qp-evidence

echo "== 4/4 firewall (SSH only for now; web ports are opened in a later milestone)"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable
ufw status verbose

echo
echo "Users:"; for u in qp-web qp-worker qp-sandbox; do id "$u"; done
echo; ls -ld /srv/qp/uploads /srv/qp/staging /etc/qp
if [[ -f /var/run/reboot-required ]]; then
  echo; echo "NOTE: a reboot is required (kernel update). Run 'sudo reboot', reconnect, then run step 2."
fi
echo; echo "STEP 1 DONE"
