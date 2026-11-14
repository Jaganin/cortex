#!/bin/bash
# Bootstrap script for cortex
# Run as root on Cortex
set -euo pipefail

INSTALL_DIR="/opt/cortex-internet"
REPO_URL="https://github.com/Jaganin/cortex.git"

echo "=== cortex bootstrap ==="

# --- Docker ---
if ! command -v docker &>/dev/null; then
  echo "[+] Installing Docker..."
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
else
  echo "[✓] Docker already installed: $(docker --version)"
fi

# --- Clone or update repo ---
if [ -d "$INSTALL_DIR/.git" ]; then
  echo "[+] Updating existing install..."
  git -C "$INSTALL_DIR" pull
else
  echo "[+] Cloning repository..."
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# --- .env ---
if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "[!] .env created — FILL IT IN before starting:"
  echo "    nano $INSTALL_DIR/.env"
  echo ""
  echo "    Required values:"
  echo "      DUCKDNS_TOKEN              (from https://www.duckdns.org)"
  echo "      AUTHELIA_JWT_SECRET        (openssl rand -hex 32)"
  echo "      AUTHELIA_SESSION_SECRET    (openssl rand -hex 32)"
  echo "      AUTHELIA_STORAGE_ENCRYPTION_KEY (openssl rand -hex 32)"
  exit 0
fi

# --- acme.json ---
mkdir -p traefik
touch traefik/acme.json
chmod 600 traefik/acme.json

# --- Open firewall ports ---
echo "[+] Opening ports 80 and 443..."
ufw allow 80/tcp
ufw allow 443/tcp

# --- Start stack ---
echo "[+] Starting stack..."
docker compose pull
docker compose up -d

echo ""
echo "=== Done ==="
echo "    Traefik dashboard: https://traefik.jaganin.duckdns.org"
echo "    Authelia portal:   https://auth.jaganin.duckdns.org"
echo ""
echo "Next: create the Authelia user password"
echo "  docker run --rm authelia/authelia:latest authelia crypto hash generate argon2 --password 'yourpassword'"
echo "  Then paste the hash into: $INSTALL_DIR/authelia/users_database.yml"
