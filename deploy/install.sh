#!/usr/bin/env bash
# zaloclaw-studio — one-shot install for a NEW machine (systemd --user).
#
# Sets up: Python venv + pip deps, bridge npm deps, data/ + logs/ dirs,
# and two systemd --user services (panel=campaign, g2g).
#
# Idempotent: safe to re-run (updates deps, re-renders units, reloads systemd).
#
# Usage:
#   deploy/install.sh [--host HOST] [--panel-port N] [--g2g-port N] [--no-start]
#
# Defaults: HOST=127.0.0.1  PANEL_PORT=18090  G2G_PORT=18091
#
# Prereqs: Linux with systemd, python3 (venv), node + npm (Node >= 18).
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HOST="127.0.0.1"
PANEL_PORT="18090"
G2G_PORT="18091"
START=1

while [ $# -gt 0 ]; do
  case "$1" in
    --host)       HOST="$2"; shift 2 ;;
    --panel-port) PANEL_PORT="$2"; shift 2 ;;
    --g2g-port)   G2G_PORT="$2"; shift 2 ;;
    --no-start)   START=0; shift ;;
    -h|--help)    sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERR\033[0m %s\n' "$*" >&2; exit 1; }

say "app dir : $APP_DIR"

# --- prereqs ---------------------------------------------------------------
command -v python3 >/dev/null || die "python3 not found"
PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
say "python  : $PYV"
command -v node >/dev/null || die "node not found (install Node >= 18)"
say "node    : $(node --version)"
command -v npm  >/dev/null || die "npm not found"
command -v systemctl >/dev/null || die "systemctl not found (systemd required for Option A)"

# --- python venv + deps ----------------------------------------------------
if [ ! -x "$APP_DIR/webapp/.venv/bin/python" ]; then
  say "creating venv webapp/.venv"
  python3 -m venv "$APP_DIR/webapp/.venv"
fi
say "installing python deps"
"$APP_DIR/webapp/.venv/bin/pip" install --upgrade pip >/dev/null
"$APP_DIR/webapp/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# --- bridge node deps ------------------------------------------------------
say "installing bridge node deps"
( cd "$APP_DIR/bridge"
  if [ -f package-lock.json ]; then npm ci; else npm install; fi )

# --- runtime dirs ----------------------------------------------------------
mkdir -p "$APP_DIR/data" "$APP_DIR/logs"

# --- render + install the two systemd --user units -------------------------
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
say "rendering systemd units -> $UNIT_DIR"

render() {  # <template> <dest>
  sed -e "s#__APP_DIR__#$APP_DIR#g" \
      -e "s#__HOME_DIR__#$HOME#g" \
      -e "s#__HOST__#$HOST#g" \
      -e "s#__PANEL_PORT__#$PANEL_PORT#g" \
      -e "s#__G2G_PORT__#$G2G_PORT#g" \
      "$1" >"$2"
  echo "    wrote $(basename "$2")"
}
render "$APP_DIR/deploy/zaloclaw-studio-panel.service.tmpl" "$UNIT_DIR/zaloclaw-studio-panel.service"
render "$APP_DIR/deploy/zaloclaw-g2g.service.tmpl"           "$UNIT_DIR/zaloclaw-g2g.service"

systemctl --user daemon-reload

if [ "$START" -eq 1 ]; then
  say "enabling + starting services (survives reboot via user linger)"
  systemctl --user enable --now zaloclaw-studio-panel.service zaloclaw-g2g.service
  # keep user services running after logout / on boot without an active login
  if command -v loginctl >/dev/null; then
    loginctl enable-linger "$USER" 2>/dev/null || say "note: could not enable-linger (may need sudo)"
  fi
  sleep 3
  for u in zaloclaw-studio-panel zaloclaw-g2g; do
    printf '    %-24s %s\n' "$u" "$(systemctl --user is-active "$u")"
  done
else
  say "skipping start (--no-start). To start later:"
  echo "    systemctl --user enable --now zaloclaw-studio-panel.service zaloclaw-g2g.service"
fi

cat <<EOF

$(say "done")

  Panel (campaign) : http://$HOST:$PANEL_PORT/
  G2G   pipeline   : http://$HOST:$G2G_PORT/

  Logs : journalctl --user -u zaloclaw-studio-panel -f
         journalctl --user -u zaloclaw-g2g -f

  First run: open the panel -> tab "Tài khoản" -> "Thêm tài khoản" -> scan the
  Zalo QR. Credentials are stored under data/accounts/ (never committed).
EOF
