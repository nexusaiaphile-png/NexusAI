#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/dist"
WORK="$ROOT/macos/.build"
PKGROOT="$WORK/pkgroot"
SCRIPTS="$WORK/scripts"

rm -rf "$WORK" "$OUT"
mkdir -p "$PKGROOT" "$SCRIPTS" "$OUT"

cat > "$SCRIPTS/postinstall" <<'POSTINSTALL'
#!/bin/bash
set -euo pipefail
CONSOLE_USER="$(stat -f '%Su' /dev/console)"
if [ -z "$CONSOLE_USER" ] || [ "$CONSOLE_USER" = "root" ]; then exit 0; fi
USER_HOME="$(dscl . -read /Users/"$CONSOLE_USER" NFSHomeDirectory | awk '{print $2}')"
TARGET="$USER_HOME/Library/Application Support/NexusAI/EdgeAgent"
mkdir -p "$TARGET"
curl -fsSL "https://raw.githubusercontent.com/nexusaiaphile-png/NexusAI/main/edge_agent/agent.py" -o "$TARGET/agent.py"
curl -fsSL "https://raw.githubusercontent.com/nexusaiaphile-png/NexusAI/main/edge_agent/requirements.txt" -o "$TARGET/requirements.txt"
if [ ! -x "$TARGET/.venv/bin/python" ]; then
  /usr/bin/python3 -m venv "$TARGET/.venv"
fi
"$TARGET/.venv/bin/python" -m pip install --upgrade pip
"$TARGET/.venv/bin/python" -m pip install -r "$TARGET/requirements.txt"
cat > "$TARGET/.env" <<EOF
NEXUSAI_API_URL=https://nexusai-worker.onrender.com
NEXUSAI_EDGE_TOKEN=
LOCAL_AGENT_HOST=0.0.0.0
LOCAL_AGENT_PORT=8787
EOF
chmod 700 "$TARGET"
chmod 600 "$TARGET/.env"
chown -R "$CONSOLE_USER":staff "$TARGET"

PLIST="$USER_HOME/Library/LaunchAgents/co.nexusai.edge-agent.plist"
mkdir -p "$(dirname "$PLIST")"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>co.nexusai.edge-agent</string>
<key>ProgramArguments</key><array><string>$TARGET/.venv/bin/python</string><string>$TARGET/agent.py</string></array>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>$TARGET/agent.out.log</string>
<key>StandardErrorPath</key><string>$TARGET/agent.err.log</string>
</dict></plist>
EOF
chown "$CONSOLE_USER":staff "$PLIST"
chmod 644 "$PLIST"
launchctl bootout "gui/$(id -u "$CONSOLE_USER")" "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u "$CONSOLE_USER")" "$PLIST"
launchctl kickstart -k "gui/$(id -u "$CONSOLE_USER")/co.nexusai.edge-agent"
echo "NexusAI Edge Agent installed and configured for automatic startup."
POSTINSTALL
chmod +x "$SCRIPTS/postinstall"

pkgbuild --root "$PKGROOT" --scripts "$SCRIPTS" --identifier "co.nexusai.edge-agent" --version "1.5.0" --install-location "/" "$OUT/NexusAI-Edge-Agent-macOS-unsigned.pkg"
echo "Built: $OUT/NexusAI-Edge-Agent-macOS-unsigned.pkg"
