#!/bin/sh
# Builds "Screen Mask.app" next to this script.
# Needs the Xcode command line tools (run: xcode-select --install) and macOS 12.3 or later.
set -e
cd "$(dirname "$0")"

APP="Screen Mask.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

swiftc -O -swift-version 5 main.swift -o "$APP/Contents/MacOS/ScreenMask"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Screen Mask</string>
    <key>CFBundleDisplayName</key><string>Screen Mask</string>
    <key>CFBundleIdentifier</key><string>io.github.screenmask.ScreenMask</string>
    <key>CFBundleExecutable</key><string>ScreenMask</string>
    <key>CFBundleIconFile</key><string>ScreenMask</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>0.1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSMinimumSystemVersion</key><string>12.3</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSPrincipalClass</key><string>NSApplication</string>
</dict>
</plist>
PLIST

# The icon is optional: skip it quietly if the tools are not there.
(
    [ -f icon.png ] || exit 0
    SET="$(mktemp -d)/ScreenMask.iconset"
    mkdir -p "$SET"
    for s in 16 32 128 256 512; do
        sips -z "$s" "$s" icon.png --out "$SET/icon_${s}x${s}.png" >/dev/null
        d=$((s * 2))
        sips -z "$d" "$d" icon.png --out "$SET/icon_${s}x${s}@2x.png" >/dev/null
    done
    iconutil -c icns "$SET" -o "$APP/Contents/Resources/ScreenMask.icns"
) 2>/dev/null || echo "(icon skipped)"

# Sign it for this Mac, so macOS can remember the screen-recording permission.
codesign --force --sign - "$APP"

echo "Built: $(pwd)/$APP"
echo "Start it with:  open \"$(pwd)/$APP\""
