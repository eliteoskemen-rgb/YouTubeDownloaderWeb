#!/bin/bash

set -e

echo "========================================"
echo "YouTube Downloader starting..."
echo "========================================"

echo "Node:"
node --version || true

echo "yt-dlp:"
yt-dlp --version || true

echo "Starting BGUTIL PO Token Provider..."

node /opt/bgutil/build/main.js &
POT_PID=$!

sleep 3

echo "Testing PO Token provider..."

curl -fsS http://127.0.0.1:4416/ping || true

echo
echo "Starting FastAPI..."

exec uvicorn server:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}"