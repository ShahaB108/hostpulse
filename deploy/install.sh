#!/usr/bin/env bash
# =============================================================================
# HostPulse one-click installer
#
# Installs the HostPulse collector (stdlib-only, no pip dependencies) as a
# periodic systemd service, with Prometheus textfile output enabled by
# default (the primary consumption path). The FastAPI web panel is OPT-IN
# via --web (it is the only part needing pip packages and a venv).
#
# Usage:
#   sudo bash deploy/install.sh [options]        # from inside the repo checkout
#
# Remote bootstrap (the URL must be the RAW content of this script; see
# README.md "One-click install"):
#   curl -fsSL https://raw.githubusercontent.com/ShahaB108/hostpulse/main/deploy/install.sh | sudo bash
#   curl -fsSL .../install.sh | sudo bash -s -- --web
#
# All flags have HOSTPULSE_* environment-variable equivalents so they can be
# passed through `curl | bash -s --` one-liners or systemd/call-wrappers; the
# environment variables win for a one-line migration (see next comment).
# =============================================================================
set -euo pipefail

# -----------------------------------------------------------------------------
# EDIT THIS ONE LINE when the repo moves (e.g. personal GitHub -> organization
# GitLab) and nowhere else. This URL is only needed when the installer has to
# download the code itself; running it from inside a checkout never downloads.
# Expected shapes (tarball of a branch):
#   GitHub:  https://github.com/OWNER/REPO/archive/refs/heads/BRANCH.tar.gz
#   GitLab:  https://gitlab.example.com/GROUP/REPO/-/archive/BRANCH/REPO-BRANCH.tar.gz
# -----------------------------------------------------------------------------
DEFAULT_SOURCE="https://github.com/ShahaB108/hostpulse/archive/refs/heads/main.tar.gz"

INSTALL_DIR="${HOSTPULSE_INSTALL_DIR:-/opt/hostpulse}"
SOURCE_URL="${HOSTPULSE_SOURCE:-$DEFAULT_SOURCE}"

# Prometheus output is the default goal of this install; point it at
# node_exporter's textfile collector directory.
DEFAULT_PROM_PATH="/var/lib/node_exporter/textfile_collector/hostpulse.prom"
ENABLE_PROM=1
PROM_PATH="${HOSTPULSE_PROM_PATH:-$DEFAULT_PROM_PATH}"

# The web panel is opt-in (set env HOSTPULSE_INSTALL_WEB=1 for curl|bash).
INSTALL_WEB=0
[ "${HOSTPULSE_INSTALL_WEB:-0}" = "1" ] && INSTALL_WEB=1

WEB_PORT="${HOSTPULSE_WEB_PORT:-35707}"
ADMIN_USER="${HOSTPULSE_ADMIN_USER:-admin}"
ADMIN_USER="${HOSTPULSE_WEB_USER:-$ADMIN_USER}"
HTPASSWD_FILE="${INSTALL_DIR}/config/htpasswd"

# Timer/cron cadence. systemd accepts anything OnUnitActiveSec does ("45min",
# "90min", "2h"...). The cron fallback understands Ng and Nmin.
INTERVAL="${HOSTPULSE_INTERVAL:-1h}"
EXTRA_IGNORED="${HOSTPULSE_IGNORED_USERS:-}"

PURGE=0

log()  { echo "==> $*"; }
warn() { echo "WARNING: $*" >&2; }
die()  { echo "ERROR: $*" >&2; exit 1; }

usage() {
  cat <<'EOF'
HostPulse one-click installer

Defaults: collector only + hourly systemd timer + Prometheus textfile output
          (no venv, no pip). Use --web to also install the FastAPI panel.

Options (env-var equivalent in parentheses):
  --source URL        tarball URL of the code        (HOSTPULSE_SOURCE)
                      Only needed when running remotely; running from inside
                      a checkout never downloads.
  --install-dir DIR   install location, default /opt/hostpulse
                                                     (HOSTPULSE_INSTALL_DIR)
  --prom PATH         Prometheus textfile output path (HOSTPULSE_PROM_PATH)
                      default: /var/lib/node_exporter/textfile_collector/hostpulse.prom
  --no-prom           disable Prometheus output      (HOSTPULSE_PROM_PATH=/dev/null no;
                      use --prom "" instead)
  --web               also install the web panel     (HOSTPULSE_INSTALL_WEB=1)
  --no-web            force the web panel off
  --web-port N        panel port, default 35707      (HOSTPULSE_WEB_PORT)
  --admin USER        panel login name, default admin (HOSTPULSE_ADMIN_USER)
                      A RANDOM password is generated on first install and
                      printed in the final summary.
  --interval T        collection cadence, default 1h (HOSTPULSE_INTERVAL)
  --ignore-users A,B extra system accounts to exclude (HOSTPULSE_IGNORED_USERS)
  --uninstall         stop and remove services/timers/units (keep files)
  --purge             with --uninstall: also delete the install directory,
                      including hostpulse.env, reports, state and htpasswd
  -h, --help          this help

Examples:
  sudo bash deploy/install.sh                     # collector + Prometheus
  sudo bash deploy/install.sh --web               # + web panel on :35707
  sudo bash deploy/install.sh --no-prom --interval 15min
  sudo bash deploy/install.sh --uninstall --purge
EOF
}

# -----------------------------------------------------------------------------
# Argument parsing
# -----------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --source)       [ $# -ge 2 ] || die "--source needs a URL"; SOURCE_URL="$2"; shift 2 ;;
    --source=*)     SOURCE_URL="${1#*=}"; shift ;;
    --install-dir)  [ $# -ge 2 ] || die "--install-dir needs a path"; INSTALL_DIR="$2"; shift 2 ;;
    --install-dir=*) INSTALL_DIR="${1#*=}"; shift ;;
    --prom)         [ $# -ge 2 ] || die "--prom needs a path or an empty value"; PROM_PATH="$2"; ENABLE_PROM=1; shift 2 ;;
    --prom=*)       PROM_PATH="${1#*=}"; shift ;;
    --no-prom)      ENABLE_PROM=0; PROM_PATH=""; shift ;;
    --web)          INSTALL_WEB=1; shift ;;
    --no-web)       INSTALL_WEB=0; shift ;;
    --web-port)     [ $# -ge 2 ] || die "--web-port needs a number"; WEB_PORT="$2"; shift 2 ;;
    --admin)        [ $# -ge 2 ] || die "--admin needs a username"; ADMIN_USER="$2"; shift 2 ;;
    --interval)     [ $# -ge 2 ] || die "--interval needs a value"; INTERVAL="$2"; shift 2 ;;
    --ignore-users) [ $# -ge 2 ] || die "--ignore-users needs a comma-separated list"; EXTRA_IGNORED="$2"; shift 2 ;;
    --uninstall)    DO_UNINSTALL=1; shift ;;
    --purge)        PURGE=1; shift ;;
    -h|--help)      usage; exit 0 ;;
    *)              usage >&2; die "unknown option: $1" ;;
  esac
done

[ "${DO_UNINSTALL:-0}" = "1" ] || [ "$(id -u)" -eq 0 ] || \
  die "This installer must run as root (sudo)."

# -----------------------------------------------------------------------------
# Uninstall path
# -----------------------------------------------------------------------------
uninstall() {
  local unit
  if command -v systemctl >/dev/null 2>&1; then
    for unit in hostpulse-web.service hostpulse-collect.service hostpulse.timer; do
      if [ -f "/etc/systemd/system/${unit}" ]; then
        systemctl stop "${unit}" 2>/dev/null || true
        systemctl disable "${unit}" 2>/dev/null || true
        rm -f "/etc/systemd/system/${unit}"
        log "Removed unit ${unit}"
      fi
    done
    systemctl daemon-reload 2>/dev/null || true
  fi
  if [ -f /etc/cron.d/hostpulse ]; then
    rm -f /etc/cron.d/hostpulse
    log "Removed /etc/cron.d/hostpulse"
  fi
  if [ "$PURGE" = "1" ]; then
    case "$INSTALL_DIR" in
      "/"|""|"/usr"|"/etc"|"/var"|"/home") die "Refusing to purge unsafe path: ${INSTALL_DIR}" ;;
    esac
    rm -rf "$INSTALL_DIR"
    log "Purged ${INSTALL_DIR} (config, reports, state, htpasswd removed)"
  else
    log "Kept ${INSTALL_DIR} (use --purge to delete it, including hostpulse.env and htpasswd)"
  fi
  log "Uninstall done."
  exit 0
}

[ "${DO_UNINSTALL:-0}" = "1" ] && uninstall

# -----------------------------------------------------------------------------
# Source resolution: local checkout wins, otherwise download the tarball.
# When piped via curl|bash, $BASH_SOURCE may point at /dev/stdin -- the -d
# check below fails safely there and forces the download path.
# -----------------------------------------------------------------------------
resolve_source() {
  local script_dir tmp
  script_dir="$(dirname "$(readlink -f "${BASH_SOURCE[0]:-/dev/stdin}" 2>/dev/null || echo /dev/stdin)" 2>/dev/null || true)"
  # When piped via curl|bash the script's "path" resolves to a pipe, so this
  # cd fails and the download branch below takes over.
  script_dir="$(cd "${script_dir}/.." 2>/dev/null && pwd)" || script_dir=""

  if [ -n "$script_dir" ] && [ -f "${script_dir}/hostpulse.py" ] && [ -f "${script_dir}/hostpulse.env.sample" ]; then
    SRC_DIR="$script_dir"
    log "Source: local checkout at ${SRC_DIR} (no download)"
    return
  fi

  # Remote bootstrap: downloader must exist (curl | bash guarantees one).
  local fetched=""
  if command -v curl >/dev/null 2>&1; then fetched="curl -fsSL"
  elif command -v wget >/dev/null 2>&1; then fetched="wget -qO-"
  fi
  [ -n "$fetched" ] || die "Neither curl nor wget available to download ${SOURCE_URL}"

  command -v tar >/dev/null 2>&1 || die "tar is required to unpack the source tarball"
  tmp="$(mktemp -d /tmp/hostpulse-install.XXXXXX)"
  trap 'rm -rf "${CLEAR_TMP:-}"' EXIT
  CLEAR_TMP="$tmp"
  log "Downloading source from ${SOURCE_URL}"
  $fetched "$SOURCE_URL" | tar -xz -C "$tmp"
  SRC_DIR="$(dirname "$(find "$tmp" -maxdepth 3 -name hostpulse.py | head -n 1)")"
  [ -n "$SRC_DIR" ] || die "Downloaded tarball does not contain hostpulse.py -- wrong URL?"
  log "Source: downloaded to ${SRC_DIR}"
}

# -----------------------------------------------------------------------------
# Preflight
# -----------------------------------------------------------------------------
PY3="$(command -v python3 || true)"
[ -n "$PY3" ] || die "python3 not found -- HostPulse needs Python 3."
if ! "$PY3" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 6) else 1)'; then
  die "Python >= 3.6 required (found: $($PY3 -V 2>&1))"
fi
PY3="$(readlink -f "$PY3")"

SYSTEMD=0
if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
  SYSTEMD=1
else
  warn "systemd not detected -- falling back to /etc/cron.d/hostpulse instead of a timer."
fi

# -----------------------------------------------------------------------------
# Install the application files
# -----------------------------------------------------------------------------
upsert_env() {
  # upsert_env KEY VALUE -- replace "KEY=..." in an env file or append it.
  local key="$1" value="$2" file="$3"
  if grep -q "^${key}=" "$file"; then
    sed -i "s#^${key}=.*#${key}=${value}#" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

install_files() {
  mkdir -p "$INSTALL_DIR/collectors"
  cp -f "${SRC_DIR}/hostpulse.py" "${SRC_DIR}/hostpulse.env.sample" "$INSTALL_DIR/"
  cp -a "${SRC_DIR}/collectors/." "$INSTALL_DIR/collectors/"
  rm -rf "${INSTALL_DIR}/collectors/__pycache__"

  if [ "$INSTALL_WEB" = "1" ]; then
    cp -f "${SRC_DIR}/hostpulse_web.py" "${SRC_DIR}/requirements.txt" "$INSTALL_DIR/"
  fi

  if [ ! -f "${INSTALL_DIR}/hostpulse.env" ]; then
    cp "${SRC_DIR}/hostpulse.env.sample" "${INSTALL_DIR}/hostpulse.env"
    log "Created ${INSTALL_DIR}/hostpulse.env from the sample"
    # Point the env file's absolute paths at the chosen install dir. Only on
    # first creation -- on re-runs an existing env file is never overwritten.
    if [ "$INSTALL_DIR" != "/opt/hostpulse" ]; then
      sed -i "s#^HOSTPULSE_LOG_FILE=.*#HOSTPULSE_LOG_FILE=${INSTALL_DIR}/logs/hostpulse.log#" "${INSTALL_DIR}/hostpulse.env"
      sed -i "s#^HOSTPULSE_JSON_OUTPUT=.*#HOSTPULSE_JSON_OUTPUT=${INSTALL_DIR}/output/users.json#" "${INSTALL_DIR}/hostpulse.env"
      sed -i "s#^HOSTPULSE_VHOST_STATE_FILE=.*#HOSTPULSE_VHOST_STATE_FILE=${INSTALL_DIR}/state/vhost_traffic.json#" "${INSTALL_DIR}/hostpulse.env"
    fi
  else
    log "Keeping existing ${INSTALL_DIR}/hostpulse.env"
  fi

  # Prometheus output: on by default, configured via the single env var the
  # collector and (optionally) the web panel both read.
  if [ "$ENABLE_PROM" = "1" ]; then
    upsert_env HOSTPULSE_PROM_OUTPUT "$PROM_PATH" "${INSTALL_DIR}/hostpulse.env"
    mkdir -p "$(dirname "$PROM_PATH")"
    log "Prometheus textfile output: ${PROM_PATH}"
  else
    upsert_env HOSTPULSE_PROM_OUTPUT "" "${INSTALL_DIR}/hostpulse.env"
    log "Prometheus textfile output disabled"
  fi

  if [ -n "$EXTRA_IGNORED" ]; then
    upsert_env HOSTPULSE_IGNORED_USERS "$EXTRA_IGNORED" "${INSTALL_DIR}/hostpulse.env"
    log "Extra ignored users: ${EXTRA_IGNORED}"
  fi

  mkdir -p "${INSTALL_DIR}/logs" "${INSTALL_DIR}/output" "${INSTALL_DIR}/state" "${INSTALL_DIR}/config"
}

# Run the collector once so /users.json and the .prom file exist immediately
# after install (no 503 / missing metrics on first scrape). Collect errors
# are fine on non-CloudLinux boxes: the report still gets written.
first_run() {
  log "Running the collector once to verify the setup"
  ( cd "$INSTALL_DIR" && "$PY3" "./hostpulse.py" ) || \
    warn "First collector run exited nonzero -- check ${INSTALL_DIR}/logs/hostpulse.log"
}

install_web_panel() {
  log "Web panel: creating venv and installing dependencies"
  if ! "$PY3" -m venv "${INSTALL_DIR}/.venv"; then
    die "'python3 -m venv' failed (ensurepip missing? Debian/Ubuntu: apt install python3-venv -- RHEL/CloudLinux: dnf install python3)"
  fi
  "${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip >/dev/null
  "${INSTALL_DIR}/.venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

  mkdir -p "$(dirname "$HTPASSWD_FILE")"
  if [ -f "$HTPASSWD_FILE" ]; then
    log "Web panel: existing htpasswd kept (forgot the password? see summary)"
  else
    # Always-generated random password (no TTY guarantees under curl|bash).
    # NOTE: read a bounded chunk of /dev/urandom and consume the whole
    # pipeline with tail -- `tr | head` would SIGPIPE the producer and,
    # combined with set -euo pipefail, kill the installer silently.
    local password
    password="$(head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | tail -c 20)"
    [ "${#password}" -eq 20 ] || die "could not generate a random password"
    printf '%s\n' "$password" | "${INSTALL_DIR}/.venv/bin/python" \
      "${INSTALL_DIR}/hostpulse_web.py" --add-user "$ADMIN_USER" \
      --htpasswd "$HTPASSWD_FILE" --password-stdin >/dev/null
    GENERATED_PASSWORD="$password"
    log "Web panel: user '${ADMIN_USER}' created in ${HTPASSWD_FILE}"
  fi
}

install_units() {
  local unit
  local -a units=(hostpulse-collect.service hostpulse.timer)
  if [ "$INSTALL_WEB" = "1" ]; then
    units+=(hostpulse-web.service)
  fi

  mkdir -p /etc/systemd/system
  for unit in "${units[@]}"; do
    if [ "$INSTALL_DIR" != "/opt/hostpulse" ]; then
      sed "s#/opt/hostpulse#${INSTALL_DIR}#g" "${SRC_DIR}/deploy/${unit}" > "/etc/systemd/system/${unit}"
    else
      cp "${SRC_DIR}/deploy/${unit}" "/etc/systemd/system/${unit}"
    fi
    # The collect unit hardcodes /usr/bin/python3 -- point it at the real one.
    if [ "$PY3" != "/usr/bin/python3" ] && [ "${unit}" = "hostpulse-collect.service" ]; then
      sed -i "s#^ExecStart=/usr/bin/python3#ExecStart=${PY3}#" "/etc/systemd/system/${unit}"
    fi
    # Point the web unit at the configured port when it is not the default.
    if [ "$WEB_PORT" != "35707" ] && [ "$unit" = "hostpulse-web.service" ]; then
      sed -i "s@--port 35707@--port ${WEB_PORT}@g" "/etc/systemd/system/${unit}"
    fi
    # Respect the requested collection cadence.
    if [ "$INTERVAL" != "1h" ] && [ "$unit" = "hostpulse.timer" ]; then
      sed -i "s#^OnUnitActiveSec=.*#OnUnitActiveSec=${INTERVAL}#" "/etc/systemd/system/${unit}"
    fi
  done
  log "Installed systemd units: ${units[*]}"
}

install_cron_fallback() {
  local spec="0 * * * *"
  case "$INTERVAL" in
    [0-9]*min) spec="*/${INTERVAL%%min} * * * *" ;;
    [0-9]*h)   spec="0 */${INTERVAL%%h} * * *" ;;
    *) warn "Unrecognized interval '${INTERVAL}' for cron -- defaulting to hourly." ;;
  esac
  cat > /etc/cron.d/hostpulse <<EOF
# HostPulse collector (installed by deploy/install.sh; systemd not detected)
${spec} root ${PY3} ${INSTALL_DIR}/hostpulse.py
EOF
  chmod 0644 /etc/cron.d/hostpulse
  log "Installed /etc/cron.d/hostpulse (${spec})"
}

enable_services() {
  systemctl daemon-reload
  systemctl enable --now hostpulse.timer
  if [ "$INSTALL_WEB" = "1" ]; then
    systemctl enable --now hostpulse-web.service
  fi
}

summary() {
  local host
  host="$(hostname -f 2>/dev/null || hostname)"
  echo
  echo "HostPulse ${VERSION:-} installed at ${INSTALL_DIR}"
  echo "  Report      : ${INSTALL_DIR}/output/users.json"
  if [ "$ENABLE_PROM" = "1" ]; then
    echo "  Prometheus  : ${PROM_PATH}"
    echo "                (served by node_exporter's textfile collector once it runs)"
  fi
  if [ "$SYSTEMD" = "1" ]; then
    echo "  Timer       : systemctl list-timers hostpulse.timer"
  else
    echo "  Cron        : /etc/cron.d/hostpulse"
  fi
  echo "  Logs        : ${INSTALL_DIR}/logs/hostpulse.log"
  if [ "$INSTALL_WEB" = "1" ]; then
    echo "  Web panel   : http://${host}:${WEB_PORT}/  (Basic Auth)"
    echo "  Admin user  : ${ADMIN_USER}"
    if [ -n "${GENERATED_PASSWORD:-}" ]; then
      echo "  Password    : ${GENERATED_PASSWORD}"
      echo "                STORE IT NOW -- it is not written anywhere else."
    else
      echo "  Password    : existing htpasswd kept; reset with:"
      echo "                printf '%s\\n' 'NEWPASS' | ${INSTALL_DIR}/.venv/bin/python ${INSTALL_DIR}/hostpulse_web.py --add-user ${ADMIN_USER} --htpasswd ${HTPASSWD_FILE} --password-stdin"
    fi
  fi
  echo
  echo "Next steps:"
  echo "  - Review ${INSTALL_DIR}/hostpulse.env (lveinfo syntax, thresholds, ignored users)."
  echo "  - See README.md -> 'Feeding this into Grafana' for dashboards."
  echo "  - Re-run this installer anytime to update the code; config/htpasswd/reports are kept."
}

GENERATED_PASSWORD=""

resolve_source
VERSION="$(sed -n 's#^__version__ *= *"\([^"]*\)".*#\1#p' "${SRC_DIR}/hostpulse.py" | head -n 1)"
install_files
first_run
if [ "$INSTALL_WEB" = "1" ]; then
  install_web_panel
fi

if [ "$SYSTEMD" = "1" ]; then
  install_units
  enable_services
else
  install_cron_fallback
fi

summary
