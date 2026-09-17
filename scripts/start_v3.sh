#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY_BIN="${XIAODUAN_PYTHON:-python}"
HOST="${XIAODUAN_V3_HOST:-0.0.0.0}"
PORT="${XIAODUAN_V3_PORT:-6010}"

cd "$ROOT_DIR"
exec "$PY_BIN" -m uvicorn app.v3.main:app --host "$HOST" --port "$PORT"
