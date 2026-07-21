#!/usr/bin/env bash
set -euo pipefail

# Bamai packet capture permission setup, based on Wireshark's access_bpf approach.
# Bamai 抓包权限设置，采用与 Wireshark access_bpf 相同的专用用户组方案。
# This never grants Bamai root access; it only makes /dev/bpf* readable by access_bpf.
# 本脚本不会让 Bamai 以 root 运行，只允许 access_bpf 组读取 /dev/bpf*。

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 sudo 运行此脚本。 / Run this script with sudo."
  exit 1
fi

TARGET_USER="${SUDO_USER:-}"
if [[ -z "$TARGET_USER" || "$TARGET_USER" == root ]]; then
  echo "无法确定要授权的登录用户。 / Could not determine the login user to authorize."
  exit 1
fi

GROUP_NAME=access_bpf
HELPER_DIR="/Library/Application Support/Bamai"
HELPER_PATH="$HELPER_DIR/ChmodBPF"
PLIST_PATH="/Library/LaunchDaemons/com.bamai.access-bpf.plist"

mkdir -p "$HELPER_DIR"
if ! dscl . -read "/Groups/$GROUP_NAME" >/dev/null 2>&1; then
  dseditgroup -o create "$GROUP_NAME"
  touch "$HELPER_DIR/created-access-bpf-group"
fi
if ! dseditgroup -o checkmember -m "$TARGET_USER" "$GROUP_NAME" | grep -q 'yes'; then
  dseditgroup -o edit -a "$TARGET_USER" -t user "$GROUP_NAME"
  touch "$HELPER_DIR/added-user-$TARGET_USER"
fi

cat > "$HELPER_PATH" <<'HELPER'
#!/usr/bin/env bash
# Give only packet-capture headers to members of access_bpf. / 仅向 access_bpf 组开放抓包设备。
/usr/bin/chgrp access_bpf /dev/bpf* 2>/dev/null || true
/bin/chmod 0640 /dev/bpf* 2>/dev/null || true
HELPER
chown root:wheel "$HELPER_PATH"
chmod 0755 "$HELPER_PATH"

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.bamai.access-bpf</string>
  <key>ProgramArguments</key><array><string>$HELPER_PATH</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
PLIST
chown root:wheel "$PLIST_PATH"
chmod 0644 "$PLIST_PATH"

launchctl bootout system/com.bamai.access-bpf >/dev/null 2>&1 || true
launchctl bootstrap system "$PLIST_PATH"
"$HELPER_PATH"

echo "抓包权限已启用；请退出登录后重新登录使组成员身份生效。 / Capture access enabled; log out and back in to activate group membership."
echo "撤销命令：sudo ./scripts/disable-capture.sh / To undo: sudo ./scripts/disable-capture.sh"
