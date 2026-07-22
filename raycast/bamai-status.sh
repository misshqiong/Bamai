#!/usr/bin/env bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Bamai Status
# @raycast.mode fullOutput

# Optional parameters:
# @raycast.icon 📊
# @raycast.packageName Bamai

# Documentation:
# @raycast.description 查看 Bamai 服务、端口、Ollama 与模型状态 / Show Bamai service, port, Ollama, and model status

exec "$(cd "$(dirname "$0")/.." && pwd)/bamai" status
