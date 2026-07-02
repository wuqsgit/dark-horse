#!/bin/bash
# DarkHorse startup script.
# Export BINANCE_API_KEY and BINANCE_API_SECRET before running this script.
export BINANCE_TESTNET="${BINANCE_TESTNET:-true}"

cd "$(dirname "$0")"
source .env 2>/dev/null

echo "🐎 DarkHorse Starting (含实盘 Testnet)..."
echo ""

# Kill existing
kill 2>/dev/null $(cat /tmp/alphadog_*.pid)
sleep 1

# Start all services
python3 pipeline/main.py > /tmp/alphadog_pipeline.log 2>&1 &
echo $! > /tmp/alphadog_pipeline.pid
echo "  ✅ Pipeline (每10min行情 + 每30min链上)"

python3 engine/run.py > /tmp/alphadog_engine.log 2>&1 &
echo $! > /tmp/alphadog_engine.pid
echo "  ✅ Engine (每5min评分 + 每1h回测)"

sleep 1

uvicorn api.main:app --host 0.0.0.0 --port 8000 > /tmp/alphadog_api.log 2>&1 &
echo $! > /tmp/alphadog_api.pid
echo "  ✅ API"

python3 trader/runner.py > /tmp/alphadog_trader.log 2>&1 &
echo $! > /tmp/alphadog_trader.pid
echo "  ✅ Trader (每5min轮询, Testnet)"

cd frontend
npx vite --host 0.0.0.0 --port 3000 > /tmp/alphadog_frontend.log 2>&1 &
echo $! > /tmp/alphadog_frontend.pid
cd ..

echo ""
echo "  📊 Frontend: http://localhost:3000"
echo "  🔌 API:      http://localhost:8000"
echo "  💹 实盘:     Testnet"
echo ""
echo "  📋 Logs: tail -f /tmp/alphadog_{pipeline,engine,api,trader,frontend}.log"
echo "  🛑 Stop:  kill \$(cat /tmp/alphadog_pipeline.pid) \$(cat /tmp/alphadog_engine.pid) \$(cat /tmp/alphadog_api.pid) \$(cat /tmp/alphadog_trader.pid) \$(cat /tmp/alphadog_frontend.pid)"
