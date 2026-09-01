#!/bin/sh

set -e

echo "=========================================="
echo "Starting BgUtils PO Token Provider..."
echo "=========================================="

node /opt/bgutil/server/build/main.js --port 4416 &

BGUTIL_PID=$!

echo "BgUtils PID: $BGUTIL_PID"

sleep 3

echo "=========================================="
echo "Checking BgUtils..."
echo "=========================================="

if wget -q -O - http://127.0.0.1:4416/ping >/dev/null 2>&1; then
    echo "BgUtils: OK"
else
    echo "BgUtils: started, ping unavailable"
fi

echo "=========================================="
echo "Starting FastAPI..."
echo "=========================================="

exec uvicorn server:app \
    --host 0.0.0.0 \
    --port "${PORT:-10000}"
