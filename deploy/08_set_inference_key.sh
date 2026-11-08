#!/usr/bin/env bash
# STEP 8 - store the Vultr Serverless Inference key so ONLY qp-worker receives it.
#   sudo bash deploy/08_set_inference_key.sh
# The key is typed (or pasted) at a hidden prompt. It is never echoed, never put in an
# environment variable, a shell history, a config file or the database. systemd passes a
# private copy to qp-worker at start-up (LoadCredential=).
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }
install -d -o root -g root -m 0700 /etc/qp-secrets
printf "Paste the Vultr inference API key, then press Enter (nothing will be shown): "
IFS= read -rs key
echo
key="$(printf '%s' "$key" | tr -d '[:space:]')"
[[ ${#key} -ge 16 ]] || { echo "That does not look like a key (too short). Nothing saved."; exit 1; }
(umask 077; printf '%s' "$key" > /etc/qp-secrets/inference_key.new)
unset key
mv /etc/qp-secrets/inference_key.new /etc/qp-secrets/inference_key
chmod 0600 /etc/qp-secrets/inference_key
echo "Saved: $(stat -c '%U:%G %a' /etc/qp-secrets/inference_key) /etc/qp-secrets/inference_key"
echo "== checking the key with a real chat request (listing models alone does not prove the key; prints no secrets)"
QP_INFERENCE_KEY_FILE=/etc/qp-secrets/inference_key python3 -I -B /opt/qp/app/tools/inference_check.py --list \
  --ping glm-5.3-flash@low || { echo "KEY CHECK FAILED - is this the inference subscription's API key (not its ID, not the account key)?"; exit 1; }
systemctl restart qp-worker
sleep 2
systemctl --no-pager --lines=0 status qp-worker || true
journalctl -u qp-worker --no-pager -n 3 -o cat
echo "STEP 8 DONE"
