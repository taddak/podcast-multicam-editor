#!/usr/bin/env bash
# Installer for Podcast Multicam Editor (macOS / Linux).
# Copies the script into DaVinci Resolve's user scripts folder.
set -euo pipefail

RAW_URL="https://raw.githubusercontent.com/taddak/podcast-multicam-editor/main/Podcast%20Multicam%20Editor.py"
NAME="Podcast Multicam Editor.py"

case "$(uname -s)" in
    Darwin)
        TARGET_DIR="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit"
        FFMPEG_HINT="brew install ffmpeg"
        ;;
    Linux)
        TARGET_DIR="$HOME/.local/share/DaVinciResolve/Fusion/Scripts/Edit"
        FFMPEG_HINT="sudo apt install ffmpeg"
        ;;
    *)
        echo "Unsupported OS: $(uname -s). On Windows use install.ps1." >&2
        exit 1
        ;;
esac

mkdir -p "$TARGET_DIR"

# prefer a local copy (running from a cloned repo / unpacked zip)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd || true)"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/$NAME" ]; then
    cp "$SCRIPT_DIR/$NAME" "$TARGET_DIR/$NAME"
    echo "Installed from local copy."
else
    curl -fsSL "$RAW_URL" -o "$TARGET_DIR/$NAME"
    echo "Downloaded the latest version."
fi

echo "Installed to: $TARGET_DIR/$NAME"

if ! command -v ffmpeg >/dev/null 2>&1 \
        && [ ! -x /opt/homebrew/bin/ffmpeg ] \
        && [ ! -x /usr/local/bin/ffmpeg ]; then
    echo
    echo "NOTE: ffmpeg was not found - the script needs it for audio analysis."
    echo "Install it with: $FFMPEG_HINT"
fi

echo
echo "Done! In DaVinci Resolve (Studio) open your synced multicam timeline"
echo "and run: Workspace -> Scripts -> Edit -> Podcast Multicam Editor"
