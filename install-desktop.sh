#!/usr/bin/env bash
# Install (or refresh) the "RCX Env Launcher" entry in your app menu / desktop.
#
# Generates a .desktop file pointing at THIS copy of the repo — wherever it
# lives — and drops it in ~/.local/share/applications/ so it shows up in the
# app menu and can be pinned to the dock. Safe to re-run any time.
#
#   ./install-desktop.sh            # install / update the launcher
#   ./install-desktop.sh --uninstall  # remove it again
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPS="$HOME/.local/share/applications"
DEST="$APPS/rcx-launcher.desktop"

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -f "$DEST"
  command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" 2>/dev/null || true
  echo "Removed $DEST"
  exit 0
fi

# Pick an icon that exists (SVG preferred, PNG fallback, else none).
ICON="$DIR/web/icon.svg"
[[ -f "$ICON" ]] || ICON="$DIR/web/icon.png"
[[ -f "$ICON" ]] || ICON="utilities-terminal"   # generic stock icon fallback

chmod +x "$DIR/start.sh"
mkdir -p "$APPS"

cat > "$DEST" <<EOF
[Desktop Entry]
Type=Application
Name=RCX Env Launcher
Comment=Open client/env Kibana, UI & NiFi links, auto-connecting the right VPN
Exec=$DIR/start.sh
Icon=$ICON
Terminal=false
Categories=Development;
StartupWMClass=RCX-Launcher
EOF
chmod 644 "$DEST"

# Validate + refresh the menu cache (both optional, non-fatal).
command -v desktop-file-validate >/dev/null 2>&1 && desktop-file-validate "$DEST" || true
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" 2>/dev/null || true

echo "Installed: $DEST"
echo "  → launches: $DIR/start.sh"
echo "  → icon:     $ICON"
echo
echo 'Search "RCX Env Launcher" in your app menu, then right-click its icon to'
echo 'pin it to the dock / favorites. (Log out and back in if it does not appear.)'
