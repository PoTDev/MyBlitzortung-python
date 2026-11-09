#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this installer as root: sudo bash deploy/install.sh" >&2
  exit 1
fi

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

if [[ ! -f compose.yaml || ! -f .env.example ]]; then
  echo "Run this script from a complete MyBlitzortung project checkout." >&2
  exit 1
fi

echo "Choose how people will open MyBlitzortung:"
echo "  1) Public IP and HTTP (no domain; not encrypted)"
echo "  2) Domain and HTTPS (recommended)"
read -rp "Choose 1 or 2 [1]: " MODE
MODE="${MODE:-1}"

case "$MODE" in
  1)
    read -rp "Enter this server's public IPv4 address: " SERVER_IP
    python3 - "$SERVER_IP" <<'PY'
import ipaddress
import sys
try:
    address = ipaddress.IPv4Address(sys.argv[1])
except ipaddress.AddressValueError:
    raise SystemExit("Enter a valid public IPv4 address.")
if not address.is_global:
    raise SystemExit("Enter the server's public (globally routable) IPv4 address.")
PY
    ORIGIN="http://${SERVER_IP}:8080"
    BIND_HOST="0.0.0.0"
    SECURE_COOKIE="false"
    ;;
  2)
    read -rp "Enter your domain name (for example lightning.example.org): " DOMAIN
    python3 - "$DOMAIN" <<'PY'
import re
import sys
domain = sys.argv[1]
if len(domain) > 253 or not re.fullmatch(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*",
    domain,
):
    raise SystemExit("Enter a domain name only, without scheme, path, or port.")
PY
    DOMAIN="${DOMAIN,,}"
    read -rp "Email for Let's Encrypt certificate notices: " CERT_EMAIL
    if [[ -z "$CERT_EMAIL" ]]; then
      echo "An email address is required to request the HTTPS certificate." >&2
      exit 1
    fi
    ORIGIN="https://${DOMAIN}"
    BIND_HOST="127.0.0.1"
    SECURE_COOKIE="true"
    ;;
  *)
    echo "Choose 1 or 2." >&2
    exit 1
    ;;
esac

if [[ ! -f .env ]]; then
  cp .env.example .env
  python3 - <<'PY'
from pathlib import Path
import re
import secrets

path = Path(".env")
content = path.read_text()
values = {
    "MARIADB_ROOT_PASSWORD": secrets.token_hex(32),
    "MARIADB_PASSWORD": secrets.token_hex(32),
}
for key, value in values.items():
    content, count = re.subn(rf"^{key}=.*$", f"{key}={value}", content, count=1, flags=re.MULTILINE)
    if count != 1:
        raise SystemExit(f"Missing {key} in .env.example")
path.write_text(content)
PY
fi
chmod 600 .env

export MYBO_ORIGIN="$ORIGIN"
export MYBO_BASE_PATH=""
export MYBO_BIND_HOST="$BIND_HOST"
export MYBO_BIND_PORT="8080"
export MYBO_SECURE_COOKIE="$SECURE_COOKIE"
python3 - <<'PY'
from pathlib import Path
import os
import re

path = Path(".env")
content = path.read_text()
values = {
    key: os.environ[key]
    for key in ("MYBO_ORIGIN", "MYBO_BASE_PATH", "MYBO_BIND_HOST", "MYBO_BIND_PORT", "MYBO_SECURE_COOKIE")
}
for key, value in values.items():
    content, count = re.subn(rf"^{key}=.*$", f"{key}={value}", content, count=1, flags=re.MULTILINE)
    if count != 1:
        content += f"\n{key}={value}\n"
path.write_text(content)
PY

echo "Installing Docker Engine and Compose..."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2
systemctl enable --now docker
docker compose version

echo "Building and starting MyBlitzortung..."
docker compose config --quiet
docker compose up -d --build --wait --wait-timeout 180 api worker

if [[ "$MODE" == "2" ]]; then
  echo "Installing Nginx and Certbot..."
  DEBIAN_FRONTEND=noninteractive apt-get install -y nginx certbot
  systemctl enable --now nginx

  install -d -m 755 /etc/nginx/snippets /var/www/mybo-acme/.well-known/acme-challenge
  install -m 644 deploy/nginx-rate.conf /etc/nginx/conf.d/mybo-rate.conf
  install -m 644 deploy/nginx-proxy-common.conf /etc/nginx/snippets/mybo-proxy-common.conf
  SITE_FILE=/etc/nginx/sites-available/myblitzortung
  cat >"$SITE_FILE" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/mybo-acme;
    }
    location / {
        return 503;
    }
}
EOF
  ln -sfn "$SITE_FILE" /etc/nginx/sites-enabled/myblitzortung
  nginx -t
  systemctl reload nginx

  echo "Requesting an HTTPS certificate for ${DOMAIN}..."
  certbot certonly --webroot -w /var/www/mybo-acme -d "$DOMAIN" \
    --email "$CERT_EMAIL" --agree-tos --non-interactive --keep-until-expiring

  cat >"$SITE_FILE" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/mybo-acme;
    }
    location / {
        return 308 https://\$host\$request_uri;
    }
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name ${DOMAIN};

    ssl_certificate /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    location / {
        include /etc/nginx/snippets/mybo-proxy-common.conf;
        proxy_pass http://127.0.0.1:8080;
    }
}
EOF
  install -d -m 755 /etc/letsencrypt/renewal-hooks/deploy
  cat >/etc/letsencrypt/renewal-hooks/deploy/reload-nginx <<'EOF'
#!/bin/sh
systemctl reload nginx
EOF
  chmod 755 /etc/letsencrypt/renewal-hooks/deploy/reload-nginx
  nginx -t
  systemctl reload nginx
fi

USER_COUNT="$(docker compose exec -T api python -c \
  'from sqlalchemy import select; from mybo.config import Config; from mybo.db import engine_for, users; e=engine_for(Config.from_env().database); c=e.connect(); print(c.scalar(select(users.c.id).limit(1)) or ""); c.close(); e.dispose()')"
if [[ -z "$USER_COUNT" ]]; then
  read -rp "Create the first site administrator? Login [operator]: " ADMIN_LOGIN
  ADMIN_LOGIN="${ADMIN_LOGIN:-operator}"
  docker compose run --rm init python -m mybo.cli create-admin "$ADMIN_LOGIN"
else
  echo "Existing site users were found; keeping them and the database unchanged."
fi

echo
echo "Installation complete."
echo "Open: ${ORIGIN}"
if [[ "$MODE" == "1" ]]; then
  echo "HTTP is not encrypted. Allow inbound TCP port 8080 in your hosting firewall."
else
  echo "HTTPS certificate auto-renewal is configured. Allow inbound TCP ports 80 and 443."
fi
docker compose ps
