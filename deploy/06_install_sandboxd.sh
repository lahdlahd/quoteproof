#!/usr/bin/env bash
# STEP 6 - install and start qp-sandboxd under systemd.
#   sudo bash deploy/06_install_sandboxd.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }
install -o root -g root -m 0644 /opt/qp/app/deploy/qp-sandboxd.service /etc/systemd/system/qp-sandboxd.service
systemctl daemon-reload
systemctl enable qp-sandboxd
systemctl restart qp-sandboxd
sleep 3
systemctl --no-pager --lines=0 status qp-sandboxd || true
echo "== recent log"
journalctl -u qp-sandboxd --no-pager -n 20 -o cat
echo "== socket"
ls -ld /run/qp-sandboxd
ls -l /run/qp-sandboxd/sandboxd.sock
echo "== network listeners owned by qp-sandbox (expected: none)"
ss -ltnup | grep -i qp-sandbox || echo "none"
systemctl is-active --quiet qp-sandboxd && echo "STEP 6 DONE" || { echo "qp-sandboxd is NOT running - paste the log above to Claude"; exit 1; }
