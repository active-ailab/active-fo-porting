#!/usr/bin/env bash
# One-click launcher backend. Starts the portal from its own directory and
# waits until the HTTP API is ready before returning to the desktop launcher.
set -u

PORT="${1:-8770}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$ROOT/feature-portal-server.log"

cd "$ROOT" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found" >&2
  exit 127
fi

if curl -fsS "http://127.0.0.1:$PORT/api/ping" >/dev/null 2>&1; then
  echo "ready=1 port=$PORT"
  exit 0
fi

nohup python3 -u server.py --port "$PORT" --idle-exit 8 >>"$LOG" 2>&1 &
pid=$!
for _ in $(seq 1 100); do
  if curl -fsS "http://127.0.0.1:$PORT/api/ping" >/dev/null 2>&1; then
    ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    echo "ready=1 pid=$pid port=$PORT ip=${ip:-127.0.0.1}"
    exit 0
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "server exited; see $LOG" >&2
    exit 1
  fi
  sleep 0.1
done
echo "server startup timeout; see $LOG" >&2
exit 1
