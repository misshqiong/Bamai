#!/usr/bin/env bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Open Bamai
# @raycast.mode compact

# Optional parameters:
# @raycast.icon 🩺
# @raycast.packageName Bamai

# Documentation:
# @raycast.description 打开 Bamai 控制台（未运行则先启动服务） / Open the Bamai dashboard (starts the service if needed)

exec "$(cd "$(dirname "$0")/.." && pwd)/bamai" start
