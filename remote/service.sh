#!/bin/sh
set -e

DATA_DIR="${WS2M_DATA_DIR:-/app/data}"
mkdir -p "$DATA_DIR"

exec python3 /app/__main__.py "$@"
