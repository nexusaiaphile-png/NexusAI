#!/bin/sh
set -eu

INSTALL_DIR="/Library/Application Support/NexusAI/SecurityBox"
PLIST="/Library/LaunchDaemons/za.co.getnexusai.securitybox.plist"
EXEC="$INSTALL_DIR/NexusAI-SecurityBox"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this installer with sudo." >&2
  exit 1
fi

mkdir -p "$INSTALL_DIR"
if [ ! -x "$EXEC" ]; then
  echo "NexusAI Security Box executable not found at $EXEC" >&2
  exit 1
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>za.co.getnexusai.securitybox</string>
  <key>ProgramArguments</key>
  <array>
    <string>$EXEC</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ProcessType</key>
  <string>Background</string>
  <key>StandardOutPath</key>
  <string>/Library/Application Support/NexusAI/SecurityBox/security-box.stdout.log</string>
  <key>StandardErrorPath</key>
  <string>/Library/Application Support/NexusAI/SecurityBox/security-box.stderr.log</string>
</dict>
</plist>
EOF

chmod 644 "$PLIST"
chown root:wheel "$PLIST"
launchctl bootout system "$PLIST" 2>/dev/null || true
launchctl bootstrap system "$PLIST"
launchctl enable system/za.co.getnexusai.securitybox
launchctl kickstart -k system/za.co.getnexusai.securitybox

echo "NexusAI Security Box installed and running."
