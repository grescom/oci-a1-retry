#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
PIDFILE="$ROOT/oci-a1.pid"
PYTHON="$ROOT/venv/bin/python"
LOG="$ROOT/attempts.log"
DAEMON_OUT="$ROOT/daemon.out"

if [[ ! -x "$PYTHON" ]]; then
  echo "Python venv is missing. Creating it and installing the OCI SDK…"
  python3 -m venv "$ROOT/venv"
  "$ROOT/venv/bin/pip" install --upgrade pip
  "$ROOT/venv/bin/pip" install -r "$ROOT/requirements.txt"
fi

if [[ ! -f "$ROOT/oci_config" ]]; then
  echo "Missing $ROOT/oci_config"
  echo "Copy oci_config.example, add your API key details, and place the private key next to it."
  echo "See README.md — do not paste the private key into chat."
  exit 2
fi

if [[ ! -f "$ROOT/settings.env" ]]; then
  echo "Missing $ROOT/settings.env — copy settings.env.example and fill in the values."
  echo "Tip: ./venv/bin/python check_setup.py --create-network --write-settings"
  exit 2
fi

if [[ -f "$PIDFILE" ]]; then
  old="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [[ -n "${old}" ]] && kill -0 "$old" 2>/dev/null; then
    echo "Already running (pid $old). Logs: $LOG"
    exit 0
  fi
  rm -f "$PIDFILE"
fi

touch "$LOG" "$DAEMON_OUT"
nohup "$PYTHON" "$ROOT/launch_a1.py" >> "$DAEMON_OUT" 2>&1 &
echo $! > "$PIDFILE"
echo "Started Ampere A1 retry loop (pid $(cat "$PIDFILE"))."
echo "  attempts log: $LOG"
echo "  daemon stdout: $DAEMON_OUT"
echo "  stop with:     $ROOT/stop.sh"
