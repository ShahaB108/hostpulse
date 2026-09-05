#!/usr/bin/env bash
# Install the HostPulse web service (FastAPI JSON API on port 35707, protected
# by an htpasswd file) plus the hourly systemd timer that refreshes the report.
#
# Usage (as root):
#   sudo bash deploy/install-web.sh
# Optional overrides:
#   INSTALL_DIR=/srv/hostpulse sudo -E bash deploy/install-web.sh
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/hostpulse}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HTPASSWD_FILE="${HTPASSWD_FILE:-/opt/hostpulse/config/htpasswd}"

if [ "$(id -u)" -ne 0 ]; then
  echo "This script must run as root." >&2
  exit 1
fi

# The repo may already live in the install directory (e.g. it was copied or
# cloned straight to /opt/hostpulse and this script is run from there). In
# that case copying files onto themselves fails with GNU cp's "same file"
# error, so skip the copy entirely -- the files are already in place.
if [ "${REPO_DIR}" = "${INSTALL_DIR}" ]; then
  echo "==> Source directory is the install directory (${INSTALL_DIR}) -- skipping file copy"
else
  echo "==> Installing application files to ${INSTALL_DIR}"
  mkdir -p "${INSTALL_DIR}/collectors"
  cp -f "${REPO_DIR}/hostpulse.py" "${REPO_DIR}/hostpulse_web.py" \
        "${REPO_DIR}/requirements.txt" "${INSTALL_DIR}/"
  # Merge-copy (cp -a src/. dst/) so an existing collectors/ directory is
  # updated in place instead of nested as collectors/collectors.
  cp -a "${REPO_DIR}/collectors/." "${INSTALL_DIR}/collectors/"
  rm -rf "${INSTALL_DIR}/collectors/__pycache__"
fi

if [ ! -f "${INSTALL_DIR}/hostpulse.env" ]; then
  cp "${REPO_DIR}/hostpulse.env.sample" "${INSTALL_DIR}/hostpulse.env"
  echo "    Created ${INSTALL_DIR}/hostpulse.env from the sample -- review it."
fi

echo "==> Creating virtualenv and installing Python dependencies"
if ! python3 -m venv "${INSTALL_DIR}/.venv"; then
  echo "ERROR: 'python3 -m venv' failed (ensurepip missing?)." >&2
  echo "       Debian/Ubuntu: apt install python3-venv" >&2
  echo "       RHEL/CloudLinux: dnf install python3" >&2
  exit 1
fi
"${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip >/dev/null
"${INSTALL_DIR}/.venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

echo "==> Preparing htpasswd file at ${HTPASSWD_FILE}"
mkdir -p "$(dirname "${HTPASSWD_FILE}")"
if [ ! -f "${HTPASSWD_FILE}" ]; then
  echo "    No htpasswd file found; creating one (set the password when prompted)."
  "${INSTALL_DIR}/.venv/bin/python" "${INSTALL_DIR}/hostpulse_web.py" \
    --add-user admin --htpasswd "${HTPASSWD_FILE}"
else
  echo "    Existing htpasswd kept. Add users with:"
  echo "      ${INSTALL_DIR}/.venv/bin/python ${INSTALL_DIR}/hostpulse_web.py --add-user USERNAME --htpasswd ${HTPASSWD_FILE}"
fi

echo "==> Installing systemd units"
for unit in hostpulse-web.service hostpulse-collect.service hostpulse.timer; do
  if [ "${INSTALL_DIR}" != "/opt/hostpulse" ]; then
    sed "s|/opt/hostpulse|${INSTALL_DIR}|g" "${REPO_DIR}/deploy/${unit}" > "/etc/systemd/system/${unit}"
  else
    cp "${REPO_DIR}/deploy/${unit}" "/etc/systemd/system/${unit}"
  fi
done

systemctl daemon-reload
systemctl enable --now hostpulse-web.service
systemctl enable --now hostpulse.timer

echo
echo "Done."
echo "  Web service : http://$(hostname -f 2>/dev/null || hostname):35707/  (Basic Auth)"
echo "  Timer       : systemctl list-timers hostpulse.timer"
echo "  Logs        : journalctl -u hostpulse-web.service -u hostpulse-collect.service -f"
echo "  Test        : curl -u admin http://127.0.0.1:35707/"
