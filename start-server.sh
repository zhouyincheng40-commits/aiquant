#!/usr/bin/env bash
# POLY Terminal static server (Linux / cloud)
set -euo pipefail
ROOT="${AIQUANT_HOME:-$(cd "$(dirname "$0")" && pwd)}"
PORT="${POLY_HTTP_PORT:-8899}"
PY="${AIQUANT_PYTHON:-python3}"
cd "$ROOT"
echo "[POLY] root=$ROOT"
echo "[POLY] python=$PY"
echo "[POLY] http://127.0.0.1:${PORT}/web/poly-terminal/index.html"
exec "$PY" -m http.server "$PORT" --bind 127.0.0.1 --directory "$ROOT"
