#!/usr/bin/env bash
# STEP 9 - public HTTPS with Caddy (automatic Let's Encrypt certificate) in front of qp-web.
#   sudo bash deploy/09_public_https.sh                 # uses <your-ip>.sslip.io
#   sudo bash deploy/09_public_https.sh quote.example.com   # if you have a domain pointing here
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }
export DEBIAN_FRONTEND=noninteractive
ip="$(curl -fsS4 https://api.ipify.org || hostname -I | awk '{print $1}')"
domain="${1:-${ip//./-}.sslip.io}"
echo "== domain: ${domain}"

if ! command -v caddy >/dev/null; then
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https >/dev/null
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update >/dev/null
  apt-get install -y caddy
fi
cat > /etc/caddy/Caddyfile <<EOF
${domain} {
	encode gzip
	request_body {
		max_size 11MB
	}
	header {
		Strict-Transport-Security "max-age=31536000"
		-Server
	}
	reverse_proxy 127.0.0.1:8080
}
EOF
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
systemctl enable caddy >/dev/null
systemctl restart caddy
echo "== waiting for the certificate (up to 60 s)"
for _ in $(seq 1 30); do
  if curl -fsS "https://${domain}/healthz" >/dev/null 2>&1; then break; fi
  sleep 2
done
curl -fsS "https://${domain}/healthz" && echo
ufw status | sed -n '1,12p'
echo "Public URL: https://${domain}/"
echo "STEP 9 DONE"
