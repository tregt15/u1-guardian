#!/usr/bin/env bash
# Install U1 Guardian on a Raspberry Pi (or any Debian-like Linux) as a systemd service.
# UNTESTED on a real Pi so far. Run it from the project folder as your normal user:
#
#     bash packaging/pi/install.sh
#
# It will ask for sudo. It does not touch your printer. It does not overwrite an existing config.
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this as your normal user, not as root (it uses sudo where needed)." >&2
  exit 1
fi

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVICE_USER="$(id -un)"
CONF_DIR=/etc/u1-guardian
DATA_DIR=/var/lib/u1-guardian
BUF_DIR=/dev/shm/u1-guardian   # RAM disk, so the SD card is only written when an incident happens

echo "==> Installing system packages (ffmpeg, python venv)"
sudo apt-get update
sudo apt-get install -y ffmpeg python3 python3-venv python3-pip

PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python3 - <<'PY' || { echo "Python 3.11 or newer is needed. Use Raspberry Pi OS Bookworm or newer." >&2; exit 1; }
import sys
sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY
echo "    Python $PYV, $(ffmpeg -version | head -1)"

echo "==> Creating the virtual environment and installing Guardian"
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -e "$INSTALL_DIR"

echo "==> Creating folders"
sudo mkdir -p "$CONF_DIR" "$DATA_DIR"
sudo chown "$SERVICE_USER":"$SERVICE_USER" "$DATA_DIR"

if [ ! -f "$CONF_DIR/config.yaml" ]; then
  echo "==> Writing a starter config to $CONF_DIR/config.yaml"
  read -r -p "IP address of your U1 (for example 192.168.0.66): " PRINTER_IP
  sudo tee "$CONF_DIR/config.yaml" >/dev/null <<EOF
printer:
  host: ${PRINTER_IP}
  moonraker_port: 80        # verified on one U1. 7125 also answered.
camera:
  # The U1 camera only gives pictures while the Snapmaker app or the printer screen shows the camera.
  snapshot_url: http://${PRINTER_IP}/server/files/camera/monitor.jpg
storage:
  data_dir: ${DATA_DIR}
  buffer_dir: ${BUF_DIR}
api:
  bind: 0.0.0.0             # reachable from your network, so a token is required (see ${CONF_DIR}/env)
  port: 8080
notifications:
  base_url: "http://$(hostname).local:8080"
  email:
    enabled: false          # set true and add to_addrs to use email. You bring your own Resend key.
EOF
else
  echo "==> Keeping your existing $CONF_DIR/config.yaml"
fi

if [ ! -f "$CONF_DIR/env" ]; then
  echo "==> Creating $CONF_DIR/env with a random access token"
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  sudo install -m 600 /dev/null "$CONF_DIR/env"
  sudo tee "$CONF_DIR/env" >/dev/null <<EOF
GUARDIAN_API_TOKEN=${TOKEN}
# Add your own Resend key here if you want email alerts:
# GUARDIAN_RESEND_KEY=your_key_here
EOF
  sudo chown root:"$SERVICE_USER" "$CONF_DIR/env"
  sudo chmod 640 "$CONF_DIR/env"
  echo "    Your dashboard access token is: ${TOKEN}"
  echo "    (it is also saved in $CONF_DIR/env). The dashboard asks for it once."
fi

echo "==> Installing the systemd service"
sed -e "s#@USER@#${SERVICE_USER}#g" -e "s#@INSTALL_DIR@#${INSTALL_DIR}#g" \
  "$INSTALL_DIR/packaging/systemd/guardian.service" | sudo tee /etc/systemd/system/guardian.service >/dev/null
# The RAM disk folder must exist before the service starts (/dev/shm is wiped on every boot).
sudo tee /etc/tmpfiles.d/u1-guardian.conf >/dev/null <<EOF
d ${BUF_DIR} 0755 ${SERVICE_USER} ${SERVICE_USER} -
EOF
sudo systemd-tmpfiles --create /etc/tmpfiles.d/u1-guardian.conf
sudo systemctl daemon-reload
sudo systemctl enable --now guardian.service

echo
echo "Done. Check it with:   systemctl status guardian    and    journalctl -u guardian -f"
echo "Dashboard:             http://$(hostname).local:8080/   (or the Pi's IP address)"
