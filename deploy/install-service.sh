#!/usr/bin/env bash
#
# Install the RepairCam web UI as a system service, so it starts automatically
# when the recorder box boots and restarts itself if it ever crashes.
#
# Run it from the repaircam folder:
#
#     ./deploy/install-service.sh
#
# Options:
#     --port 8080      port the web UI listens on (default 8080)
#     --data-dir PATH  where recordings are stored (default ~/repaircam-data)
#     --dry-run        show what would be installed, change nothing
#
set -euo pipefail

PORT=8080
HOST=0.0.0.0
DATA_DIR=""
DRY_RUN=false

SERVICE_NAME=repaircam
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

# --------------------------------------------------------------------------
# Output helpers — this script is read by someone who is not a programmer, so
# every line should say plainly what happened.
# --------------------------------------------------------------------------

if [ -t 1 ]; then
  BOLD=$(printf '\033[1m'); RED=$(printf '\033[31m'); GREEN=$(printf '\033[32m')
  YELLOW=$(printf '\033[33m'); RESET=$(printf '\033[0m')
else
  BOLD=""; RED=""; GREEN=""; YELLOW=""; RESET=""
fi

step() { printf '\n%s==> %s%s\n' "$BOLD" "$1" "$RESET"; }
ok()   { printf '    %sOK%s   %s\n' "$GREEN" "$RESET" "$1"; }
warn() { printf '    %swarn%s %s\n' "$YELLOW" "$RESET" "$1"; }
die()  { printf '\n    %sFAILED%s %s\n\n' "$RED" "$RESET" "$1" >&2; exit 1; }

# --------------------------------------------------------------------------
# Arguments
# --------------------------------------------------------------------------

while [ $# -gt 0 ]; do
  case "$1" in
    --port)     PORT="${2:-}"; shift 2 ;;
    --host)     HOST="${2:-}"; shift 2 ;;
    --data-dir) DATA_DIR="${2:-}"; shift 2 ;;
    --dry-run)  DRY_RUN=true; shift ;;
    -h|--help)  sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)          die "Unknown option '$1'. Run with --help to see the options." ;;
  esac
done

case "$PORT" in
  ''|*[!0-9]*) die "--port must be a number, got '$PORT'." ;;
esac

# --------------------------------------------------------------------------
# Work out who and where
# --------------------------------------------------------------------------

step "Checking this machine"

command -v systemctl >/dev/null 2>&1 \
  || die "This machine does not use systemd, so it cannot install a service this way.
       The service file is for Linux (Ubuntu/Debian). On a Mac, start the web UI
       manually instead:  .venv/bin/python -m repaircam.cli web"

if [ "$(id -u)" -eq 0 ]; then
  die "Do not run this with sudo.
       Run it as your normal user:  ./deploy/install-service.sh
       It will ask for your password when it needs administrator rights."
fi

SERVICE_USER=$(id -un)
SERVICE_GROUP=$(id -gn)
REPO_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
PYTHON="${REPO_DIR}/.venv/bin/python"
TEMPLATE="${REPO_DIR}/deploy/repaircam.service"

ok "user: ${SERVICE_USER}"
ok "RepairCam folder: ${REPO_DIR}"

[ -f "$TEMPLATE" ] || die "Cannot find ${TEMPLATE}. Run this from inside the repaircam folder."

# --------------------------------------------------------------------------
# Check RepairCam itself is ready before promising systemd it will start
# --------------------------------------------------------------------------

step "Checking RepairCam is ready to run"

[ -x "$PYTHON" ] || die "The Python environment is missing.
       Create it first, from inside ${REPO_DIR}:
           python3 -m venv .venv
           .venv/bin/pip install -r requirements.txt"
ok "Python environment found"

"$PYTHON" -c "import flask" >/dev/null 2>&1 || die "Flask is not installed.
       Install it from inside ${REPO_DIR}:
           .venv/bin/pip install -r requirements.txt"
ok "Flask installed"

if command -v ffmpeg >/dev/null 2>&1; then
  ok "ffmpeg installed"
else
  warn "ffmpeg is NOT installed — the web UI will start but cannot record."
  warn "Fix it with:  sudo apt update && sudo apt install -y ffmpeg"
fi

if [ -f "${REPO_DIR}/repaircam/cameras.yaml" ]; then
  ok "cameras.yaml found"
else
  warn "repaircam/cameras.yaml does not exist yet — no benches will appear."
  warn "Create it with:  cp repaircam/cameras.example.yaml repaircam/cameras.yaml"
fi

if [ -z "$DATA_DIR" ]; then
  DATA_DIR="${REPAIRCAM_DATA_DIR:-$HOME/repaircam-data}"
fi
ok "recordings will be stored in: ${DATA_DIR}"

# Warn about something that silently breaks the service later: if the data
# directory already exists but belongs to someone else (usually because an
# earlier command was run with sudo), the service cannot write to it.
if [ -e "$DATA_DIR" ] && [ ! -w "$DATA_DIR" ]; then
  die "The folder ${DATA_DIR} exists but ${SERVICE_USER} cannot write to it.
       Fix the ownership with:
           sudo chown -R ${SERVICE_USER}:${SERVICE_GROUP} ${DATA_DIR}"
fi

# --------------------------------------------------------------------------
# Build the unit file
# --------------------------------------------------------------------------

step "Preparing the service"

UNIT_TEXT=$(sed \
  -e "s|__USER__|${SERVICE_USER}|g" \
  -e "s|__GROUP__|${SERVICE_GROUP}|g" \
  -e "s|__WORKDIR__|${REPO_DIR}|g" \
  -e "s|__DATADIR__|${DATA_DIR}|g" \
  -e "s|__PYTHON__|${PYTHON}|g" \
  -e "s|__HOST__|${HOST}|g" \
  -e "s|__PORT__|${PORT}|g" \
  "$TEMPLATE")

if printf '%s' "$UNIT_TEXT" | grep -q '__[A-Z]*__'; then
  die "The service template still has unfilled placeholders. This is a bug — do not continue."
fi
ok "service file prepared"

if [ "$DRY_RUN" = true ]; then
  step "Dry run — nothing was changed"
  printf 'Would write %s:\n\n' "$UNIT_PATH"
  printf '%s\n' "$UNIT_TEXT"
  exit 0
fi

# --------------------------------------------------------------------------
# Install and start
# --------------------------------------------------------------------------

step "Installing the service (this needs your password)"

printf '%s\n' "$UNIT_TEXT" | sudo tee "$UNIT_PATH" >/dev/null
sudo chmod 644 "$UNIT_PATH"
ok "installed to ${UNIT_PATH}"

sudo systemctl daemon-reload
ok "systemd reloaded"

sudo systemctl enable "${SERVICE_NAME}.service" >/dev/null 2>&1
ok "RepairCam will now start automatically when this machine boots"

sudo systemctl restart "${SERVICE_NAME}.service"

# Give it a moment to fail, if it is going to.
sleep 3

if ! systemctl is-active --quiet "${SERVICE_NAME}.service"; then
  printf '\n    %sThe service did not stay running.%s Its last messages:\n\n' "$RED" "$RESET" >&2
  sudo journalctl -u "${SERVICE_NAME}.service" -n 25 --no-pager >&2 || true
  die "RepairCam could not start. The messages above say why."
fi

ok "RepairCam is running"

# --------------------------------------------------------------------------
# Tell the owner what to do with it
# --------------------------------------------------------------------------

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -n "$IP" ] || IP="<this machine's IP>"

cat <<EOF

${BOLD}Done.${RESET} RepairCam is running and will start again by itself after a reboot.

Open it from any phone or tablet on the shop Wi-Fi:

    ${BOLD}http://${IP}:${PORT}${RESET}

Useful commands from now on:

    Is it running?      systemctl status repaircam
    See what it's doing journalctl -u repaircam -f
    Restart it          sudo systemctl restart repaircam
    Stop it for now     sudo systemctl stop repaircam
    Stop it for good    sudo systemctl disable --now repaircam

EOF
