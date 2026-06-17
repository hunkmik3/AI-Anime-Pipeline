#!/usr/bin/env bash
# Install + start a launchd agent so Flowboard runs 24/7 and survives reboots.
# Generates the plist with absolute paths resolved for this machine.
#
#   bash packaging/install-launchd.sh            # install + start
#   bash packaging/install-launchd.sh --uninstall
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.flowboard.server"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Đã gỡ dịch vụ ${LABEL}."
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$REPO_ROOT/logs"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${REPO_ROOT}/run-server.sh</string>
  </array>
  <key>WorkingDirectory</key><string>${REPO_ROOT}/agent</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${REPO_ROOT}/logs/server.out.log</string>
  <key>StandardErrorPath</key><string>${REPO_ROOT}/logs/server.err.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "Đã cài + chạy dịch vụ ${LABEL}."
echo "Log:   ${REPO_ROOT}/logs/server.{out,err}.log"
echo "Dừng:  launchctl unload \"$PLIST\""
