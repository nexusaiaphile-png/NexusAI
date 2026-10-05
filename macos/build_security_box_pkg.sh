#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARCH="${NEXUSAI_MAC_ARCH:-}"
VERSION="${NEXUSAI_SECURITY_BOX_VERSION:-2.1.0}"
APPLICATION_IDENTITY="${APPLE_APPLICATION_IDENTITY:-}"
INSTALLER_IDENTITY="${APPLE_INSTALLER_IDENTITY:-}"

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
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install pyinstaller requests python-dotenv

rm -rf "$WORK/pyi" "$WORK/dist"
"$VENV/bin/python" -m PyInstaller \
  --clean --noconfirm --onefile \
  --name NexusAI-SecurityBox \
  --target-arch "$PY_ARCH" \
  --osx-bundle-identifier "za.co.getnexusai.securitybox" \
  --hidden-import edge_agent.agent \
  --hidden-import edge_agent.secure_store \
  --hidden-import edge_agent.capability_engine \
  edge_agent/security_box_macos_installer.py \
  --distpath "$WORK/dist" --workpath "$WORK/pyi"

EXEC="$WORK/dist/NexusAI-SecurityBox"

# The executable is the code users actually run. Apple requires executable
# code inside a notarized package to be signed with Developer ID Application.
if [[ -n "$APPLICATION_IDENTITY" ]]; then
  codesign --force --options runtime --timestamp --sign "$APPLICATION_IDENTITY" "$EXEC"
  codesign --verify --strict --verbose=2 "$EXEC"
fi

cp "$EXEC" "$PKGROOT/Library/Application Support/NexusAI/SecurityBox/NexusAI-SecurityBox"
chmod 755 "$PKGROOT/Library/Application Support/NexusAI/SecurityBox/NexusAI-SecurityBox"

cat > "$SCRIPTS/postinstall" <<'POSTINSTALL'
#!/bin/bash
set -euo pipefail

# macOS Installer passes the full package path as $1.
# The download endpoint deliberately places the short-lived installer token
# in the filename so the package can provision the correct NexusAI site
# without exposing a terminal or asking the customer to type a token.
PACKAGE_PATH="$1"
PACKAGE_NAME="$(basename "$PACKAGE_PATH")"
TOKEN="$(printf '%s' "$PACKAGE_NAME" | sed -E 's/^NexusAI-SecurityBox-Mac-(Intel|Arm64)-([A-Za-z0-9_-]+)\.pkg$/\2/')"

if ! printf '%s' "$TOKEN" | grep -Eq '^[A-Za-z0-9_-]{40,220}$'; then
  echo "NexusAI installer token could not be read from the package filename." >&2
  exit 1
fi

ROOT="/Library/Application Support/NexusAI/SecurityBox"
EXEC="$ROOT/NexusAI-SecurityBox"
PLIST="/Library/LaunchDaemons/za.co.getnexusai.securitybox.plist"

if [[ ! -x "$EXEC" ]]; then
  echo "NexusAI Security Box executable is missing." >&2
  exit 1
fi

mkdir -p "$ROOT"
"$EXEC" --installer-token "$TOKEN"

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

# Never leave the short-lived installer token behind after provisioning.
rm -f "$ROOT/installer-token" 2>/dev/null || true

echo "NexusAI Security Box installed and running."
POSTINSTALL
chmod 755 "$SCRIPTS/postinstall"

UNSIGNED="$OUT/NexusAI-SecurityBox-Mac-$LABEL-unsigned.pkg"
pkgbuild \
  --root "$PKGROOT" \
  --scripts "$SCRIPTS" \
  --identifier "za.co.getnexusai.securitybox" \
  --version "$VERSION" \
  --install-location "/" \
  "$UNSIGNED"

if [[ -n "$INSTALLER_IDENTITY" ]]; then
  FINAL="$OUT/NexusAI-SecurityBox-Mac-$LABEL.pkg"
  productsign --sign "$INSTALLER_IDENTITY" "$UNSIGNED" "$FINAL"
  rm -f "$UNSIGNED"
  pkgutil --check-signature "$FINAL"
  echo "Built signed package: $FINAL"
else
  echo "Built unsigned package: $UNSIGNED"
  echo "Set APPLE_APPLICATION_IDENTITY and APPLE_INSTALLER_IDENTITY for a distributable Developer ID build."
fi
