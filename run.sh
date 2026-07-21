#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

if [[ -x /opt/homebrew/bin/python3 ]]; then
  PYTHON_BIN=/opt/homebrew/bin/python3
else
  PYTHON_BIN=python3
fi

if [[ ! -x .venv/bin/python ]]; then
  "$PYTHON_BIN" -m venv .venv
fi

.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt

mkdir -p web/vendor
if [[ ! -s web/vendor/echarts.min.js ]]; then
  echo "首次启动：正在下载本地 ECharts…"
  curl --fail --location --silent --show-error \
    https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js \
    --output web/vendor/echarts.min.js
fi

echo "Bamai（把脉）启动中：http://127.0.0.1:8737"
exec .venv/bin/uvicorn server.main:app --host 127.0.0.1 --port 8737
