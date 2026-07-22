#!/usr/bin/env bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Restart Bamai
# @raycast.mode compact

# Optional parameters:
# @raycast.icon 🔄
# @raycast.packageName Bamai

# Documentation:
# @raycast.description 重启 Bamai 服务 / Restart the Bamai service

exec "$(cd "$(dirname "$0")/.." && pwd)/bamai" restart
