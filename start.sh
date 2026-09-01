#!/bin/bash

set -e

echo "========================================"
echo "Starting bgutil PO Token provider..."
echo "========================================"

node /opt/bgutil/server/build/main.js &

POT_PID=$!

echo "PO Token provider PID: $POT_PID"

sleep 3

echo "========================================"
echo "Starting FastAPI..."
echo "========================================"

exec uvicorn server:app --host 0.0.0.0 --port "${PORT:-8000}"