#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARCH="$NEXUSAI_MAC_ARCH"
case "$ARCH" in
  arm64) LABEL="Arm64"; PY_ARCH="arm64" ;;
  x86_64) LABEL="Intel"; PY_ARCH="x86_64" ;;
  *) echo "Unsupported macOS architecture: $ARCH"; exit 1 ;;
esac

OUT="$ROOT/dist"
WORK="$ROOT/macos/.security-box-build-$LABEL"
PKGROOT="$WORK/pkgroot"
SCRIPTS="$WORK/scripts"
rm -rf "$WORK"
mkdir -p "$PKGROOT/Library/Application Support/NexusAI/SecurityBox" "$SCRIPTS" "$OUT"

VENV="$WORK/venv"
python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip pyinstaller requests python-dotenv

rm -rf "$WORK/pyi" "$WORK/dist"
"$VENV/bin/python" -m PyInstaller --clean --noconfirm --onefile   --name NexusAI-SecurityBox   --target-arch "$PY_ARCH"   --osx-bundle-identifier "za.co.getnexusai.securitybox"   edge_agent/security_box_macos_installer.py   --distpath "$WORK/dist" --workpath "$WORK/pyi"

cp "$WORK/dist/NexusAI-SecurityBox" "$PKGROOT/Library/Application Support/NexusAI/SecurityBox/NexusAI-SecurityBox"
chmod 755 "$PKGROOT/Library/Application Support/NexusAI/SecurityBox/NexusAI-SecurityBox"

cat > "$SCRIPTS/postinstall" <<'POSTINSTALL'
#!/bin/bash
set -euo pipefail

PKG_PATH="${PACKAGE_PATH:-}"
BASE="$(basename "$PKG_PATH" .pkg)"
TOKEN="$(printf '%s' "$BASE" | sed -E 's/^NexusAI-SecurityBox-Mac-(Intel|Arm64)-([A-Za-z0-9]+)$/\2/')"
if [ -z "$TOKEN" ] || [ "$TOKEN" = "$BASE" ]; then
  echo "NexusAI installer token could not be read from package name." >&2
  exit 1
fi

ROOT="/Library/Application Support/NexusAI/SecurityBox"
EXEC="$ROOT/NexusAI-SecurityBox"
mkdir -p "$ROOT"

"$EXEC" --installer-token "$TOKEN"

PLIST="/Library/LaunchDaemons/za.co.getnexusai.securitybox.plist"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>za.co.getnexusai.securitybox</string>
<key>ProgramArguments</key><array><string>$EXEC</string></array>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>ProcessType</key><string>Background</string>
<key>StandardOutPath</key><string>$ROOT/security-box.stdout.log</string>
<key>StandardErrorPath</key><string>$ROOT/security-box.stderr.log</string>
</dict></plist>
EOF

chown root:wheel "$PLIST"
chmod 644 "$PLIST"
launchctl bootout system "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap system "$PLIST"
launchctl enable system/za.co.getnexusai.securitybox
launchctl kickstart -k system/za.co.getnexusai.securitybox
echo "NexusAI Security Box installed and running."
POSTINSTALL
chmod 755 "$SCRIPTS/postinstall"

UNSIGNED="$OUT/NexusAI-SecurityBox-Mac-$LABEL-unsigned.pkg"
pkgbuild   --root "$PKGROOT"   --scripts "$SCRIPTS"   --identifier "za.co.getnexusai.securitybox"   --version "2.0.0"   --install-location "/"   "$UNSIGNED"

echo "Built $UNSIGNED"
