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

MISSING=0

if ! command -v python3 >/dev/null 2>&1 \
        && [ ! -d "/Library/Frameworks/Python.framework" ]; then
    MISSING=1
    echo
    echo "MISSING: Python 3 - without it Resolve hides .py scripts from the menu."
    echo "  Download and install it from: https://www.python.org/downloads/"
fi

if ! command -v ffmpeg >/dev/null 2>&1 \
        && [ ! -x /opt/homebrew/bin/ffmpeg ] \
        && [ ! -x /usr/local/bin/ffmpeg ]; then
    MISSING=1
    echo
    echo "MISSING: ffmpeg - the script needs it for the audio analysis."
    if [ "$(uname -s)" = "Darwin" ]; then
        echo "  With Homebrew: brew install ffmpeg"
        echo "  Without Homebrew: download https://evermeet.cx/ffmpeg/ and"
        echo "  move the ffmpeg file to /usr/local/bin/"
    else
        echo "  Install it with: $FFMPEG_HINT"
    fi
fi

echo
if [ "$MISSING" = "1" ]; then
    echo "Script installed. Install the missing pieces above, then restart"
    echo "DaVinci Resolve (Studio) and run:"
else
    echo "Done! In DaVinci Resolve (Studio) open your synced multicam timeline"
    echo "and run:"
fi
echo "  Workspace -> Scripts -> Edit -> Podcast Multicam Editor"
