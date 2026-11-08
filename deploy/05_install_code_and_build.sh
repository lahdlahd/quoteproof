#!/usr/bin/env bash
# STEP 5 - copy the code to /opt/qp/app (root-owned, read-only to services),
# build the two sandbox images, and write /etc/qp/sandboxd.json.
#   sudo bash deploy/05_install_code_and_build.sh gvisor          # normal
#   sudo bash deploy/05_install_code_and_build.sh runc-hardened   # FALLBACK only (gVisor failed / timebox)
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }
mode="${1:-}"
[[ "$mode" == "gvisor" || "$mode" == "runc-hardened" ]] || { echo "usage: $0 gvisor|runc-hardened"; exit 1; }
src="$(cd "$(dirname "$0")/.." && pwd)"

echo "== install code from ${src} to /opt/qp/app"
rsync -a --delete --exclude '.git' --exclude '__pycache__' --exclude '.pytest_cache' "${src}/" /opt/qp/app/
chown -R root:root /opt/qp/app
chmod -R u=rwX,go=rX /opt/qp/app

echo "== build images (base image pulled once at build time; containers never pull)"
docker build --pull -t qp-runner:m1  /opt/qp/app/images/runner
docker build --pull -t qp-indexer:m1 /opt/qp/app/images/indexer
runner_id="$(docker image inspect --format '{{.Id}}' qp-runner:m1)"
indexer_id="$(docker image inspect --format '{{.Id}}' qp-indexer:m1)"
echo "runner  image: ${runner_id}"
echo "indexer image: ${indexer_id}"

echo "== write /etc/qp/sandboxd.json (isolation_mode=${mode})"
tmp="$(mktemp)"
jq -n --arg mode "$mode" --arg r "$runner_id" --arg i "$indexer_id" '{
  isolation_mode: $mode,
  images: {runner: $r, indexer: $i},
  socket_path: "/run/qp-sandboxd/sandboxd.sock",
  uploads_dir: "/srv/qp/uploads",
  staging_dir: "/srv/qp/staging",
  uploads_owner: "qp-web",
  allowed_client_users: ["qp-worker"],
  home: "/var/lib/qp-sandbox",
  docker_bin: "/usr/bin/docker"
}' > "$tmp"
install -o root -g qp-sandbox -m 0640 "$tmp" /etc/qp/sandboxd.json
rm -f "$tmp"
cat /etc/qp/sandboxd.json

echo "== run unit tests on the VM (no Docker needed for these)"
cd /opt/qp/app && python3 -m pytest -q -p no:cacheprovider || { echo "UNIT TESTS FAILED"; exit 1; }
echo "STEP 5 DONE"
