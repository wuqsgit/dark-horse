#!/bin/bash
# AlphaDog 一键启动
cd "$(dirname "$0")"

# Load env vars
source .env 2>/dev/null

echo "🐕 AlphaDog Starting..."

# Kill existing
kill 2>/dev/null $(cat /tmp/alphadog_pipeline.pid) $(cat /tmp/alphadog_engine.pid) $(cat /tmp/alphadog_api.pid) $(cat /tmp/alphadog_frontend.pid)

# Start pipeline
python3 pipeline/main.py > /tmp/alphadog_pipeline.log 2>&1 &
echo $! > /tmp/alphadog_pipeline.pid
echo "  ✅ Pipeline (PID: $(cat /tmp/alphadog_pipeline.pid))"

# Start engine
python3 engine/run.py > /tmp/alphadog_engine.log 2>&1 &
echo $! > /tmp/alphadog_engine.pid
echo "  ✅ Engine (PID: $(cat /tmp/alphadog_engine.pid))"

# Start trader
python3 -c "
import asyncio
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath('.')), '.'))
from trader.runner import trading_loop
asyncio.run(trading_loop())
" > /tmp/alphadog_trader.log 2>&1 &
echo $! > /tmp/alphadog_trader.pid
echo "  ✅ Trader (PID: $(cat /tmp/alphadog_trader.pid))"

sleep 1

# Start API
uvicorn api.main:app --host 0.0.0.0 --port 8000 > /tmp/alphadog_api.log 2>&1 &
echo $! > /tmp/alphadog_api.pid
echo "  ✅ API (PID: $(cat /tmp/alphadog_api.pid))"

# Start frontend
cd frontend
npx vite --host 0.0.0.0 --port 3000 > /tmp/alphadog_frontend.log 2>&1 &
echo $! > /tmp/alphadog_frontend.pid
cd ..

echo ""
echo "  📊 Frontend: http://localhost:3000"
echo "  🔌 API:      http://localhost:8000"
echo "  📋 Logs:     tail -f /tmp/alphadog_*.log"
echo ""
echo "  🛑 Stop:     kill \$(cat /tmp/alphadog_pipeline.pid) \$(cat /tmp/alphadog_engine.pid) \$(cat /tmp/alphadog_api.pid) \$(cat /tmp/alphadog_frontend.pid)"
