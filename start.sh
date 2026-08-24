#!/usr/bin/env bash
set -euo pipefail

# Dark Horse one-click restart for Linux and Windows Git Bash.
ROOT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
cd "$ROOT_DIR"

if [ -f ".env" ]; then
  set -a
  source .env
  set +a
fi

RUNTIME_DIR="${DARK_HORSE_RUNTIME_DIR:-/tmp}"
mkdir -p "$RUNTIME_DIR"

IS_WINDOWS=0
NATIVE_ROOT="$ROOT_DIR"
WINDOWS_STOP_HELPER=""
WINDOWS_PRESTOP_DONE=0
if [ -x ".venv/Scripts/python.exe" ]; then
  IS_WINDOWS=1
  PYTHON_BIN="$ROOT_DIR/.venv/Scripts/python.exe"
  if command -v cygpath >/dev/null 2>&1; then
    NATIVE_ROOT="$(cygpath -w "$ROOT_DIR")"
  elif pwd -W >/dev/null 2>&1; then
    NATIVE_ROOT="$(pwd -W)"
  elif command -v wslpath >/dev/null 2>&1; then
    NATIVE_ROOT="$(wslpath -w "$ROOT_DIR")"
  fi
  WINDOWS_STOP_HELPER="$ROOT_DIR/scripts/stop_dark_horse_processes.ps1"
  if command -v cygpath >/dev/null 2>&1; then
    WINDOWS_STOP_HELPER="$(cygpath -w "$WINDOWS_STOP_HELPER")"
  elif command -v wslpath >/dev/null 2>&1; then
    WINDOWS_STOP_HELPER="$(wslpath -w "$WINDOWS_STOP_HELPER")"
  fi
elif [ -x ".venv/bin/python" ]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
else
  PYTHON_BIN="${PYTHON_BIN:-python3}"
fi

if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "  FAIL Python 3.10+ is required (Python 3.11 recommended): $PYTHON_BIN"
  exit 1
fi
if ! "$PYTHON_BIN" -c 'import fastapi, uvicorn' >/dev/null 2>&1; then
  echo "  FAIL Python dependencies are missing for $PYTHON_BIN"
  echo "       Run: $PYTHON_BIN -m pip install -r api/requirements.txt -r ai_service/requirements.txt -r engine/requirements.txt -r pipeline/requirements.txt"
  exit 1
fi

SERVICE_HOST="${DARK_HORSE_SERVICE_HOST:-127.0.0.1}"
FRONTEND_HOST="${DARK_HORSE_FRONTEND_HOST:-127.0.0.1}"
if [ -z "${DARK_HORSE_API_TOKEN:-}" ]; then
  API_TOKEN_FILE="${DARK_HORSE_API_TOKEN_FILE:-$RUNTIME_DIR/dark_horse_api_token}"
  if [ ! -s "$API_TOKEN_FILE" ]; then
    umask 077
    "$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(32))' > "$API_TOKEN_FILE"
  fi
  DARK_HORSE_API_TOKEN="$(tr -d '\r\n' < "$API_TOKEN_FILE")"
  export DARK_HORSE_API_TOKEN
  echo "  Admin token: $API_TOKEN_FILE"
fi

echo "Dark Horse restarting..."
echo "  Python: $PYTHON_BIN"
echo "  Bind: $SERVICE_HOST (frontend: $FRONTEND_HOST)"

process_is_running() {
  local pid="$1"
  if kill -0 "$pid" 2>/dev/null; then
    return 0
  fi
  if [ "$IS_WINDOWS" -eq 1 ]; then
    tasklist.exe /FI "PID eq $pid" /NH 2>/dev/null | tr -d '\r' | grep -q "[[:space:]]$pid[[:space:]]"
    return $?
  fi
  return 1
}

stop_process_tree() {
  local pid="$1"
  [ -n "$pid" ] || return 0
  process_is_running "$pid" || return 0

  if [ "$IS_WINDOWS" -eq 1 ]; then
    kill "$pid" 2>/dev/null || true
    taskkill.exe /PID "$pid" /T /F >/dev/null 2>&1 || true
    return 0
  fi

  local child
  if command -v pgrep >/dev/null 2>&1; then
    for child in $(pgrep -P "$pid" 2>/dev/null || true); do
      stop_process_tree "$child"
    done
  fi

  kill -TERM "$pid" 2>/dev/null || true
  local attempt
  for attempt in 1 2 3 4 5; do
    process_is_running "$pid" || return 0
    sleep 0.2
  done
  kill -KILL "$pid" 2>/dev/null || true
}

stop_from_pidfile() {
  local pidfile="$1"
  if [ -f "$pidfile" ]; then
    local pid
    pid="$(tr -cd '0-9' < "$pidfile" 2>/dev/null || true)"
    if [ -n "$pid" ]; then
      stop_process_tree "$pid"
    fi
    rm -f "$pidfile"
  fi
}

stop_matching_processes() {
  local pattern="$1"
  local pid

  if [ "$IS_WINDOWS" -eq 1 ]; then
    [ "$WINDOWS_PRESTOP_DONE" -eq 1 ] && return 0
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$WINDOWS_STOP_HELPER" \
      -Root "$NATIVE_ROOT" -Pattern "$pattern" >/dev/null 2>&1 || true
    return 0
  fi

  command -v pgrep >/dev/null 2>&1 || return 0
  for pid in $(pgrep -f -- "$pattern" 2>/dev/null || true); do
    [ "$pid" = "$$" ] && continue
    local cwd=""
    if [ -e "/proc/$pid/cwd" ]; then
      cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
    elif command -v lsof >/dev/null 2>&1; then
      # macOS has no /proc filesystem. Resolve the process cwd through lsof
      # so stale workers from an older terminal are stopped as well.
      cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null \
        | sed -n 's/^n//p' | head -n 1 || true)"
    fi
    if [ "$cwd" = "$ROOT_DIR" ] || [ "$cwd" = "$ROOT_DIR/frontend" ]; then
      stop_process_tree "$pid"
    fi
  done
}

stop_port() {
  local port="$1"
  local pid

  if [ "$IS_WINDOWS" -eq 1 ]; then
    [ "$WINDOWS_PRESTOP_DONE" -eq 1 ] && return 0
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$WINDOWS_STOP_HELPER" \
      -Root "$NATIVE_ROOT" -Pattern "__dark_horse_no_process_match__" -Ports "$port" \
      >/dev/null 2>&1 || true
    return 0
  fi

  if command -v lsof >/dev/null 2>&1; then
    for pid in $(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true); do
      stop_process_tree "$pid"
    done
  elif command -v fuser >/dev/null 2>&1; then
    fuser -k "${port}/tcp" >/dev/null 2>&1 || true
  fi
}

stop_service() {
  local name="$1"
  local pidfile="$2"
  local pattern="$3"
  shift 3

  echo "  STOP $name"
  stop_from_pidfile "$pidfile"
  stop_matching_processes "$pattern"
  local port
  for port in "$@"; do
    stop_port "$port"
  done
}

start_service() {
  local name="$1"
  local pidfile="$2"
  local logfile="$3"
  shift 3

  nohup "$@" > "$logfile" 2>&1 < /dev/null &
  local pid=$!
  echo "$pid" > "$pidfile"
  sleep 0.3
  if ! process_is_running "$pid"; then
    echo "  FAIL $name (see $logfile)"
    tail -n 20 "$logfile" 2>/dev/null || true
    return 1
  fi
  echo "  OK   $name (PID: $pid)"
}

wait_for_port() {
  local port="$1"
  local name="$2"
  local logfile="$3"
  local timeout_seconds="${4:-60}"
  local attempt

  for ((attempt = 0; attempt < timeout_seconds * 2; attempt++)); do
    local ready=1
    if [ "$IS_WINDOWS" -eq 1 ]; then
      curl.exe --silent --show-error --max-time 1 \
        --output NUL "http://127.0.0.1:$port/" >/dev/null 2>&1 && ready=0
    elif (echo > "/dev/tcp/127.0.0.1/$port") >/dev/null 2>&1; then
      ready=0
    fi
    if [ "$ready" -eq 0 ]; then
      echo "  READY $name (port $port)"
      return 0
    fi
    sleep 0.5
  done

  echo "  FAIL $name did not listen on port $port (see $logfile)"
  tail -n 40 "$logfile" 2>/dev/null || true
  return 1
}

wait_for_worker_ready() {
  local name="$1"
  local pidfile="$2"
  local logfile="$3"
  local pattern="$4"
  local timeout_seconds="${5:-90}"
  local attempt pid

  for ((attempt = 0; attempt < timeout_seconds * 2; attempt++)); do
    pid="$(tr -cd '0-9' < "$pidfile" 2>/dev/null || true)"
    if [ -z "$pid" ] || ! process_is_running "$pid"; then
      echo "  FAIL $name exited during startup (see $logfile)"
      tail -n 40 "$logfile" 2>/dev/null || true
      return 1
    fi
    if grep -q -- "$pattern" "$logfile" 2>/dev/null; then
      echo "  READY $name"
      return 0
    fi
    sleep 0.5
  done

  echo "  FAIL $name did not become ready (see $logfile)"
  tail -n 40 "$logfile" 2>/dev/null || true
  return 1
}

if [ "$IS_WINDOWS" -eq 1 ]; then
  powershell.exe -NoProfile -ExecutionPolicy Bypass \
    -File "$WINDOWS_STOP_HELPER" \
    -Root "$NATIVE_ROOT" \
    -Pattern "__dark_horse_all__" \
    -Ports 3000,8000,8010 >/dev/null 2>&1 || true
  WINDOWS_PRESTOP_DONE=1
fi

stop_service "Pipeline" "$RUNTIME_DIR/alphadog_pipeline.pid" "pipeline/main.py"
stop_service "Alpha Pipeline" "$RUNTIME_DIR/alphadog_alpha_pipeline.pid" "alpha_pipeline.main"
stop_service "Minute Pipeline" "$RUNTIME_DIR/alphadog_minute_pipeline.pid" "minute_pipeline.main"
stop_service "Engine" "$RUNTIME_DIR/alphadog_engine.pid" "engine/run.py"
stop_service "Alpha Engine" "$RUNTIME_DIR/alphadog_alpha_engine.pid" "alpha_engine.run"
stop_service "AI Entry Quality" "$RUNTIME_DIR/alphadog_ai.pid" "ai_service.main:app" 8010
stop_service "Trader" "$RUNTIME_DIR/alphadog_trader.pid" "trader.runner"
stop_service "API" "$RUNTIME_DIR/alphadog_api.pid" "api.main:app" 8000
stop_service "Frontend" "$RUNTIME_DIR/alphadog_frontend.pid" "vite" 3000

start_service "AI Entry Quality" "$RUNTIME_DIR/alphadog_ai.pid" "$RUNTIME_DIR/alphadog_ai.log" \
  "$PYTHON_BIN" -m uvicorn ai_service.main:app --host "$SERVICE_HOST" --port 8010
wait_for_port 8010 "AI Entry Quality" "$RUNTIME_DIR/alphadog_ai.log" 60

start_service "API" "$RUNTIME_DIR/alphadog_api.pid" "$RUNTIME_DIR/alphadog_api.log" \
  "$PYTHON_BIN" -m uvicorn api.main:app --host "$SERVICE_HOST" --port 8000
wait_for_port 8000 "API" "$RUNTIME_DIR/alphadog_api.log" 90

start_service "Pipeline" "$RUNTIME_DIR/alphadog_pipeline.pid" "$RUNTIME_DIR/alphadog_pipeline.log" \
  "$PYTHON_BIN" pipeline/main.py
sleep 1

start_service "Alpha Pipeline" "$RUNTIME_DIR/alphadog_alpha_pipeline.pid" "$RUNTIME_DIR/alphadog_alpha_pipeline.log" \
  "$PYTHON_BIN" -m alpha_pipeline.main
sleep 1

start_service "Minute Pipeline" "$RUNTIME_DIR/alphadog_minute_pipeline.pid" "$RUNTIME_DIR/alphadog_minute_pipeline.log" \
  "$PYTHON_BIN" -m minute_pipeline.main
wait_for_worker_ready "Minute Pipeline" "$RUNTIME_DIR/alphadog_minute_pipeline.pid" \
  "$RUNTIME_DIR/alphadog_minute_pipeline.log" "minute pipeline mode=" 90
sleep 1

start_service "Engine" "$RUNTIME_DIR/alphadog_engine.pid" "$RUNTIME_DIR/alphadog_engine.log" \
  "$PYTHON_BIN" engine/run.py
sleep 1

start_service "Alpha Engine" "$RUNTIME_DIR/alphadog_alpha_engine.pid" "$RUNTIME_DIR/alphadog_alpha_engine.log" \
  "$PYTHON_BIN" -m alpha_engine.run
sleep 1

start_service "Trader" "$RUNTIME_DIR/alphadog_trader.pid" "$RUNTIME_DIR/alphadog_trader.log" \
  "$PYTHON_BIN" -m trader.runner
sleep 1

(
  cd frontend
  echo "  BUILD Frontend"
  npm run build >/tmp/alphadog_frontend_build.log 2>&1
  start_service "Frontend" "$RUNTIME_DIR/alphadog_frontend.pid" "$RUNTIME_DIR/alphadog_frontend.log" \
    npx vite preview --host "$FRONTEND_HOST" --port 3000
)
wait_for_port 3000 "Frontend" "$RUNTIME_DIR/alphadog_frontend.log" 30

echo ""
echo "  Frontend: http://localhost:3000"
echo "  API:      http://localhost:8000"
echo "  AI:       http://localhost:8010/v1/status"
echo "  Logs:     tail -f $RUNTIME_DIR/alphadog_*.log"
echo ""
echo "All Dark Horse services restarted."
