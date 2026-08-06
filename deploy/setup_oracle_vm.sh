#!/usr/bin/env bash
# Run this ON a fresh Oracle Cloud (or any Ubuntu/Debian) VM, after cloning
# the repo and cd-ing into it:
#
#   git clone https://github.com/MaxMann02/flightdiversions.git
#   cd flightdiversions
#   TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=yyy bash deploy/setup_oracle_vm.sh
#
# (Telegram vars are optional — leave them out and edit
# /etc/systemd/system/flightdiversions.service afterwards if you'd rather
# fill them in later.)
#
# Sets up a venv, installs deps, and installs+starts a systemd service that
# keeps the monitor+dashboard (serve_all.py) running 24/7, auto-restarting
# on crash and on VM reboot.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="$(whoami)"
PORT="${PORT:-8787}"
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"

sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip

python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --no-cache-dir -r "$APP_DIR/requirements.txt"

sudo tee /etc/systemd/system/flightdiversions.service > /dev/null <<EOF
[Unit]
Description=Flight Diversions Monitor
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/serve_all.py
Restart=always
RestartSec=5
Environment=PORT=$PORT
Environment=TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
Environment=TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID
User=$SERVICE_USER

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now flightdiversions

# Oracle's stock Ubuntu images ship with restrictive iptables rules on top
# of the cloud-level Security List/NSG — the #1 reason people get "port
# looks open in the console but the site is still unreachable" specifically
# on Oracle Cloud. Open it at the OS level too.
if command -v iptables >/dev/null 2>&1; then
  sudo iptables -C INPUT -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null \
    || sudo iptables -I INPUT 6 -p tcp --dport "$PORT" -j ACCEPT
  sudo netfilter-persistent save 2>/dev/null \
    || (sudo mkdir -p /etc/iptables && sudo iptables-save | sudo tee /etc/iptables/rules.v4 > /dev/null) \
    || true
fi

echo
echo "=== Status ==="
sudo systemctl status flightdiversions --no-pager || true
echo
echo "Dashboard/link: http://<PUBLIC_IP_VAN_DEZE_VM>:$PORT"
echo "(zorg dat poort $PORT ook open staat in de Oracle Cloud Security List — zie README)"
