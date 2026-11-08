#!/usr/bin/env bash
# STEP 7 (Milestone 2) - install qp-worker and qp-web, without touching Milestone 1's isolation.
#   sudo bash deploy/07_install_m2.sh
# Safe to re-run. Does NOT set the inference key (see 08_set_inference_key.sh) and does NOT
# open the firewall or install Caddy (see 09_public_https.sh).
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }
src="$(cd "$(dirname "$0")/.." && pwd)"
export DEBIAN_FRONTEND=noninteractive

echo "== 1/7 shared database group (qp-web + qp-worker only)"
getent group qp-db >/dev/null || groupadd --system qp-db
usermod -aG qp-db qp-web
usermod -aG qp-db qp-worker

echo "== 2/7 folders"
install -d -o root -g qp-db -m 2770 /srv/qp/db                   # setgid: db, -wal, -shm files stay group qp-db
install -d -o qp-worker -g qp-worker -m 0750 /srv/qp/indexes      # worker-only: validated Source Indexes
install -d -o root -g qp-worker -m 0750 /etc/qp-worker
install -d -o root -g qp-web -m 0750 /etc/qp-web
install -d -o root -g root -m 0700 /etc/qp-secrets                # root-only; systemd hands out credentials

echo "== 3/7 code (same root-owned, read-only location as Milestone 1)"
rsync -a --delete --exclude '.git' --exclude '__pycache__' --exclude '.pytest_cache' "${src}/" /opt/qp/app/
chown -R root:root /opt/qp/app
chmod -R u=rwX,go=rX /opt/qp/app

echo "== 4/7 qp-web virtual environment (hash-pinned packages only)"
apt-get install -y python3-venv >/dev/null
[[ -x /opt/qp/venv-web/bin/python ]] || python3 -m venv /opt/qp/venv-web
/opt/qp/venv-web/bin/pip install --quiet --no-deps --only-binary=:all: --require-hashes \
  -r /opt/qp/app/deploy/requirements-web.txt
/opt/qp/venv-web/bin/pip check
chown -R root:root /opt/qp/venv-web
chmod -R u=rwX,go=rX /opt/qp/venv-web

echo "== 5/7 configuration"
models_file=/etc/qp-worker/models.env
if [[ ! -f "$models_file" ]]; then
  # Placeholders until the model bake-off (tools/model_bakeoff.py) picks the real IDs.
  cat > "$models_file" <<'EOF'
EXTRACTOR=qwen3.8-27b
FALLBACK=deepseek-v4.1-flash-1017
CLASSIFIER=glm-5.3-flash
MATCHER=qwen3.8-27b
EXPLAINER=qwen3.8-27b
EOF
fi
# shellcheck disable=SC1090
. "$models_file"
tmp="$(mktemp)"
jq -n --arg e "$EXTRACTOR" --arg f "$FALLBACK" --arg c "$CLASSIFIER" --arg m "$MATCHER" --arg x "$EXPLAINER" '{
  db: "/srv/qp/db/qp.sqlite", indexes_dir: "/srv/qp/indexes",
  socket_path: "/run/qp-sandboxd/sandboxd.sock", base_url: "https://api.vultrinference.com/v1",
  models: {extractor: $e, fallback: $f, classifier: $c, matcher: $m, explainer: $x}}' > "$tmp"
install -o root -g qp-worker -m 0640 "$tmp" /etc/qp-worker/worker.json
jq -n '{db: "/srv/qp/db/qp.sqlite", uploads_dir: "/srv/qp/uploads", max_upload_bytes: 10485760,
        max_docs_per_project: 10, project_ttl_s: 86400}' > "$tmp"
install -o root -g qp-web -m 0640 "$tmp" /etc/qp-web/web.json
rm -f "$tmp"
[[ -f /etc/qp-secrets/web_secret ]] || (umask 077; head -c 48 /dev/urandom | base64 -w0 > /etc/qp-secrets/web_secret)
chmod 0600 /etc/qp-secrets/web_secret

echo "== 6/7 database schema (created as group qp-db, mode 0660)"
runuser -u qp-web -g qp-web -G qp-db -- sh -c 'umask 0007; /opt/qp/venv-web/bin/python -I -c "
import sys; sys.path.insert(0, \"/opt/qp/app\")
from qp import db; c = db.connect(\"/srv/qp/db/qp.sqlite\"); db.init(c); print(\"schema ok\")"'
chmod 0660 /srv/qp/db/qp.sqlite*

echo "== 7/7 services"
install -o root -g root -m 0644 /opt/qp/app/deploy/qp-worker.service /etc/systemd/system/qp-worker.service
install -o root -g root -m 0644 /opt/qp/app/deploy/qp-web.service /etc/systemd/system/qp-web.service
systemctl daemon-reload
systemctl enable qp-web qp-worker >/dev/null
systemctl restart qp-web
sleep 2
systemctl --no-pager --lines=0 status qp-web || true
curl -fsS http://127.0.0.1:8080/healthz && echo
if [[ -s /etc/qp-secrets/inference_key ]]; then
  systemctl restart qp-worker; sleep 2; systemctl --no-pager --lines=0 status qp-worker || true
  journalctl -u qp-worker --no-pager -n 3 -o cat
else
  echo "qp-worker not started yet: set the inference key with: sudo bash deploy/08_set_inference_key.sh"
fi
echo "== unit tests on the VM (stdlib; web tests run in the web venv)"
cd /opt/qp/app && python3 -m pytest -q -p no:cacheprovider || { echo "UNIT TESTS FAILED"; exit 1; }
echo "(web tests need httpx, which is not installed in production; they ran in Claude's workspace)"
echo "STEP 7 DONE"
