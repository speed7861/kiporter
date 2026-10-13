#!/usr/bin/env bash
# Kiporter - installer for Linux (Ubuntu/Debian/Fedora/Arch...) and macOS
#  * checks for Python 3.9+ with Tk (offers to install it)
#  * creates a private virtual environment
#  * installs dependencies
#  * adds an app-menu entry (Linux) or ~/Applications/Kiporter.app (macOS) and launches Kiporter
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OS="$(uname -s)"
if [ "$OS" = "Darwin" ]; then
    DATA_DIR="$HOME/Library/Application Support/Kiporter"
else
    DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/kiporter"
fi
VENV="$DATA_DIR/venv"
LOGO="$APP_DIR/kiporter/assets/logo.png"

say() { printf '\033[1;36m%s\033[0m\n' "$*"; }
ask() { read -r -p "$1 [Y/n] " a; [ -z "$a" ] || [ "$a" = "y" ] || [ "$a" = "Y" ]; }

python_ok() {
    "$1" -c 'import sys, tkinter, venv; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1
}

find_python() {
    for c in python3.13 python3.12 python3.11 python3.10 python3 python; do
        if command -v "$c" >/dev/null 2>&1 && python_ok "$(command -v "$c")"; then
            command -v "$c"; return 0
        fi
    done
    return 1
}

say "== Kiporter setup =="
PY="$(find_python || true)"
if [ -z "$PY" ]; then
    echo "Python 3.9+ with Tk (tkinter) and venv was not found."
    if [ "$OS" = "Darwin" ]; then
        if command -v brew >/dev/null 2>&1 && ask "Install it with Homebrew (python@3.12 + python-tk@3.12)?"; then
            brew install python@3.12 python-tk@3.12
        else
            echo "Install Python from https://www.python.org/downloads/macos/ (includes Tk) and run this again."; exit 1
        fi
    elif command -v apt-get >/dev/null 2>&1; then
        if ask "Install python3, python3-venv and python3-tk with apt (needs sudo)?"; then
            sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-tk
        fi
    elif command -v dnf >/dev/null 2>&1; then
        if ask "Install python3 and python3-tkinter with dnf (needs sudo)?"; then
            sudo dnf install -y python3 python3-tkinter
        fi
    elif command -v pacman >/dev/null 2>&1; then
        if ask "Install python and tk with pacman (needs sudo)?"; then
            sudo pacman -S --needed python tk
        fi
    fi
    PY="$(find_python || true)"
    [ -n "$PY" ] || { echo "Please install Python 3.9+ with Tk support and run ./install.sh again."; exit 1; }
fi
say "Using Python: $PY"

mkdir -p "$DATA_DIR"
if [ ! -x "$VENV/bin/python" ]; then
    say "Creating virtual environment..."
    "$PY" -m venv "$VENV"
fi
say "Installing dependencies..."
"$VENV/bin/python" -m pip install --disable-pip-version-check -q --upgrade pip
"$VENV/bin/python" -m pip install --disable-pip-version-check -q -r "$APP_DIR/requirements.txt"

LAUNCHER="$DATA_DIR/kiporter"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
exec "$VENV/bin/python" "$APP_DIR/run.pyw" "\$@"
EOF
chmod +x "$LAUNCHER"

if [ "$OS" = "Darwin" ]; then
    APP="$HOME/Applications/Kiporter.app"
    mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
    cat > "$APP/Contents/MacOS/Kiporter" <<EOF
#!/usr/bin/env bash
exec "$LAUNCHER"
EOF
    chmod +x "$APP/Contents/MacOS/Kiporter"
    if [ -f "$LOGO" ] && command -v sips >/dev/null && command -v iconutil >/dev/null; then
        ICONSET="$(mktemp -d)/kiporter.iconset"; mkdir -p "$ICONSET"
        for s in 16 32 64 128 256 512; do
            sips -z $s $s "$LOGO" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
            sips -z $((s*2)) $((s*2)) "$LOGO" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
        done
        iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/kiporter.icns" || true
    fi
    cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Kiporter</string>
  <key>CFBundleDisplayName</key><string>Kiporter</string>
  <key>CFBundleIdentifier</key><string>com.github.speed7861.kiporter</string>
  <key>CFBundleExecutable</key><string>Kiporter</string>
  <key>CFBundleIconFile</key><string>kiporter</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
EOF
    say "App created: $APP  (drag it to the Dock if you like)"
    echo "Tip: for the 'Update PCB' keyboard fallback allow Kiporter under"
    echo "     System Settings > Privacy & Security > Accessibility."
    say "Launching Kiporter..."
    open "$APP"
else
    mkdir -p "$HOME/.local/bin" "${XDG_DATA_HOME:-$HOME/.local/share}/applications"
    ln -sf "$LAUNCHER" "$HOME/.local/bin/kiporter"
    DESKTOP_FILE="${XDG_DATA_HOME:-$HOME/.local/share}/applications/kiporter.desktop"
    cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Kiporter
Comment=Search suppliers and import parts into KiCad
Exec="$LAUNCHER"
Icon=$LOGO
Terminal=false
Categories=Development;Electronics;
EOF
    chmod +x "$DESKTOP_FILE"
    if [ -d "$HOME/Desktop" ]; then
        cp "$DESKTOP_FILE" "$HOME/Desktop/kiporter.desktop"
        chmod +x "$HOME/Desktop/kiporter.desktop"
        command -v gio >/dev/null && gio set "$HOME/Desktop/kiporter.desktop" metadata::trusted true 2>/dev/null || true
    fi
    say "Menu entry created (search for 'Kiporter'); command: kiporter"
    if ! command -v xdotool >/dev/null 2>&1; then
        echo "Optional: 'sudo apt install xdotool wmctrl' enables the Update-PCB keyboard fallback and"
        echo "          open-window detection when KiCad's API is turned off."
    fi
    say "Launching Kiporter..."
    nohup "$LAUNCHER" >/dev/null 2>&1 &
fi
say "Setup complete."
