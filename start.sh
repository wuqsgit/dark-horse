#!/usr/bin/env bash
set -euo pipefail

# Dark Horse one-click startup.
cd "$(dirname "$0")"

source .env 2>/dev/null || true

echo "Dark Horse starting..."

stop_from_pidfile() {
  local pidfile="$1"
  if [ -f "$pidfile" ]; then
    local pid
    pid="$(cat "$pidfile" 2>/dev/null || true)"
    if [ -n "$pid" ]; then
      kill "$pid" 2>/dev/null || true
    fi
  fi
}

start_service() {
  local name="$1"
  local pidfile="$2"
  local logfile="$3"
  shift 3

  "$@" > "$logfile" 2>&1 &
  echo $! > "$pidfile"
  echo "  OK $name (PID: $(cat "$pidfile"))"
}

stop_from_pidfile /tmp/alphadog_pipeline.pid
stop_from_pidfile /tmp/alphadog_alpha_pipeline.pid
stop_from_pidfile /tmp/alphadog_engine.pid
stop_from_pidfile /tmp/alphadog_alpha_engine.pid
stop_from_pidfile /tmp/alphadog_trader.pid
stop_from_pidfile /tmp/alphadog_api.pid
stop_from_pidfile /tmp/alphadog_frontend.pid

start_service "Pipeline" /tmp/alphadog_pipeline.pid /tmp/alphadog_pipeline.log \
  python3 pipeline/main.py

start_service "Alpha Pipeline" /tmp/alphadog_alpha_pipeline.pid /tmp/alphadog_alpha_pipeline.log \
  python3 -m alpha_pipeline.main

start_service "Engine" /tmp/alphadog_engine.pid /tmp/alphadog_engine.log \
  python3 engine/run.py

start_service "Alpha Engine" /tmp/alphadog_alpha_engine.pid /tmp/alphadog_alpha_engine.log \
  python3 -m alpha_engine.run

start_service "Trader" /tmp/alphadog_trader.pid /tmp/alphadog_trader.log \
  python3 -m trader.runner

sleep 1

start_service "API" /tmp/alphadog_api.pid /tmp/alphadog_api.log \
  uvicorn api.main:app --host 0.0.0.0 --port 8000

(
  cd frontend
  start_service "Frontend" /tmp/alphadog_frontend.pid /tmp/alphadog_frontend.log \
    npx vite --host 0.0.0.0 --port 3000
)

echo ""
echo "  Frontend: http://localhost:3000"
echo "  API:      http://localhost:8000"
echo "  Logs:     tail -f /tmp/alphadog_*.log"
echo ""
echo "  Stop:     kill \$(cat /tmp/alphadog_pipeline.pid) \$(cat /tmp/alphadog_alpha_pipeline.pid) \$(cat /tmp/alphadog_engine.pid) \$(cat /tmp/alphadog_alpha_engine.pid) \$(cat /tmp/alphadog_trader.pid) \$(cat /tmp/alphadog_api.pid) \$(cat /tmp/alphadog_frontend.pid)"
