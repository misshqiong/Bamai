#!/usr/bin/env bash
set -euo pipefail

# Fully undo Bamai's access_bpf setup. / 完整撤销 Bamai 的 access_bpf 抓包授权。
# Existing pcap files are intentionally preserved for user-controlled deletion.
# 已有 pcap 文件会保留，由用户自行决定是否删除。

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 sudo 运行此脚本。 / Run this script with sudo."
  exit 1
fi

TARGET_USER="${SUDO_USER:-}"
GROUP_NAME=access_bpf
HELPER_DIR="/Library/Application Support/Bamai"
PLIST_PATH="/Library/LaunchDaemons/com.bamai.access-bpf.plist"

launchctl bootout system/com.bamai.access-bpf >/dev/null 2>&1 || true
CREATED_GROUP=false
[[ -f "$HELPER_DIR/created-access-bpf-group" ]] && CREATED_GROUP=true

if dscl . -read "/Groups/$GROUP_NAME" >/dev/null 2>&1; then
  if [[ -n "$TARGET_USER" && "$TARGET_USER" != root && -f "$HELPER_DIR/added-user-$TARGET_USER" ]]; then
    dseditgroup -o edit -d "$TARGET_USER" -t user "$GROUP_NAME" || true
  fi
  [[ "$CREATED_GROUP" == true ]] && dseditgroup -o delete "$GROUP_NAME"
fi

# Restore macOS-default restrictive ownership until the next device recreation.
# 恢复严格的设备权限；系统重建 bpf 设备时也会恢复默认值。
if [[ "$CREATED_GROUP" == true ]]; then
  /usr/bin/chgrp wheel /dev/bpf* 2>/dev/null || true
  /bin/chmod 0600 /dev/bpf* 2>/dev/null || true
fi
rm -f "$PLIST_PATH" "$HELPER_DIR/ChmodBPF" "$HELPER_DIR"/added-user-* "$HELPER_DIR/created-access-bpf-group"
rmdir "$HELPER_DIR" 2>/dev/null || true

echo "抓包权限已完整撤销。 / Capture access fully removed."
