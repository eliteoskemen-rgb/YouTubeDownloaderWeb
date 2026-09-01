#!/bin/sh

set -e

echo "=========================================="
echo "Starting bgutil PO Token provider"
echo "=========================================="

node /opt/bgutil/server/build/main.js \
    --port 4416 \
    > /tmp/bgutil.log 2>&1 &

POT_PID=$!

echo "PO Token provider PID: $POT_PID"

echo "Waiting for PO Token provider..."

READY=0

for i in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:4416/ping >/dev/null 2>&1; then
        READY=1
        break
    fi

    sleep 1
done

if [ "$READY" != "1" ]; then
    echo "ERROR: PO Token provider did not start"
    echo "------------------------------------------"
    cat /tmp/bgutil.log || true
    echo "------------------------------------------"
    exit 1
fi

echo "PO Token provider is READY on 127.0.0.1:4416"

echo "=========================================="
echo "Starting FastAPI"
echo "=========================================="

exec uvicorn server:app \
    --host 0.0.0.0 \
    --port "${PORT:-10000}"