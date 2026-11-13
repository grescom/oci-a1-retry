#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$ROOT/oci-a1.pid"

if [[ ! -f "$PIDFILE" ]]; then
  echo "Not running (no pid file)."
  exit 0
fi

pid="$(cat "$PIDFILE" 2>/dev/null || true)"
if [[ -z "${pid}" ]]; then
  rm -f "$PIDFILE"
  echo "Not running (empty pid file)."
  exit 0
fi

if ! kill -0 "$pid" 2>/dev/null; then
  rm -f "$PIDFILE"
  echo "Not running (stale pid $pid)."
  exit 0
fi

kill -TERM "$pid" 2>/dev/null || true
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if ! kill -0 "$pid" 2>/dev/null; then
    break
  fi
  sleep 0.3
done
if kill -0 "$pid" 2>/dev/null; then
  echo "Process $pid did not exit on SIGTERM, sending SIGKILL."
  kill -KILL "$pid" 2>/dev/null || true
fi
rm -f "$PIDFILE"
echo "Stopped pid $pid."
