#!/bin/sh

set -e

PORT="${PORT:-10000}"

echo "Starting YouTube Downloader on port ${PORT}"

exec python server.py