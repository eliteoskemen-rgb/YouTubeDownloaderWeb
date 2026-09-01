#!/bin/sh
set -e

echo "Starting YouTube Downloader on port ${PORT:-10000}"

exec python server.py