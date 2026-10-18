#!/usr/bin/env bash
# TVDebloater launcher: makes sure adb + Flask exist, then starts the server.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${TVD_PORT:-8477}"
HOST="${TVD_HOST:-127.0.0.1}"
PYTHON="${PYTHON:-python3}"

# ---------------------------------------------------------------- 1. adb --
need_adb=1
if command -v adb >/dev/null 2>&1 || [ -x tools/platform-tools/adb ] || [ -n "${ADB_PATH:-}" ]; then
  need_adb=0
fi
if [ "$need_adb" = "1" ]; then
  echo "==> adb not found - downloading Google platform-tools into ./tools (no sudo needed)"
  mkdir -p tools && cd tools
  if command -v curl >/dev/null 2>&1; then
    curl -sL -o platform-tools.zip https://dl.google.com/android/repository/platform-tools-latest-linux.zip
  else
    wget -qO platform-tools.zip https://dl.google.com/android/repository/platform-tools-latest-linux.zip
  fi
  unzip -qo platform-tools.zip && rm -f platform-tools.zip
  chmod +x platform-tools/adb
  cd ..
  echo "==> adb ready: $(tools/platform-tools/adb version | head -1)"
fi

# -------------------------------------------------------------- 2. flask --
if ! "$PYTHON" -c "import flask" >/dev/null 2>&1; then
  echo "==> Flask not found - installing it (user site-packages)"
  "$PYTHON" -m pip install --user flask || {
    echo "!! pip install failed. Try: python3 -m venv .venv && .venv/bin/pip install flask"
    echo "   then start with: .venv/bin/python app.py"
    exit 1
  }
fi

echo "==> TVDebloater starting on http://${HOST}:${PORT}"
[ -n "${TVD_MOCK:-}" ] && echo "==> MOCK MODE (fake device, nothing touches a real TV)"
exec "$PYTHON" app.py
