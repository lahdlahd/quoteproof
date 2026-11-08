#!/usr/bin/env bash
# STEP 2 - Docker Engine from Docker's official apt repository.
#   sudo bash deploy/02_install_docker.sh
# Only qp-sandbox is added to the docker group. Do NOT add your admin user to it.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }
export DEBIAN_FRONTEND=noninteractive

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
systemctl enable --now docker

usermod -aG docker qp-sandbox

echo "== verification"
docker version --format 'Docker server {{.Server.Version}}'
docker run --rm hello-world | grep -m1 "Hello from Docker"
members="$(getent group docker | cut -d: -f4)"
echo "docker group members: ${members}"
if [[ "${members}" != "qp-sandbox" ]]; then
  echo "WARNING: the docker group should contain ONLY qp-sandbox. Remove others with: gpasswd -d <user> docker"
  exit 1
fi
echo "STEP 2 DONE"
