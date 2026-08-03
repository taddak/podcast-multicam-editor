# Podcast Multicam Editor for DaVinci Resolve

Automatically cuts a multicam podcast timeline based on **who is speaking** —
an open-source alternative to AutoPod's Multi-Camera Editor, built as a native
DaVinci Resolve script.

Give it a synced timeline (one video track per camera, one audio track per
microphone) and it builds a new, fully cut timeline:

- every shot stays on **its camera's own video track** (checkerboard style),
- your **audio tracks and all audio post-processing stay untouched**
  (the cut is built on a duplicate of your timeline),
- the original timeline is never modified.

## Features

- **Speech detection tuned for podcasts** — automatic per-microphone
  thresholds (Otsu's method) that survive quiet mics, loud mics, and people
  who talk 90 % of the time.
- **Crosstalk / mic-bleed suppression** — tracks are compared against each
  other, so a mic picking up the other speaker doesn't fake overlaps.
- **Reaction cuts** — a short interjection cuts to the other speaker's
  close-up; the wide shot is used only when people genuinely talk over each
  other for a while.
- **Early cuts into pauses** — the camera lands on the next speaker a beat
  before they start talking.
- **Wide shots on monologues** at a configurable frequency, with a minimum
  wide-shot length so the edit never flashes.
- **2–10 speakers and cameras**, including group shots (a camera showing any
  subset of speakers) and multiple microphones per speaker.
- **Timeline-style UI** — video and audio tracks laid out like in Resolve,
  with per-track speaker naming and colored checkboxes.
- Settings and per-project track mappings are remembered between runs.
- Built-in **update check** — the script offers new versions as they land in
  this repository (one click to update, nothing is installed silently).

## Requirements

Three things — nothing else is needed, even on a completely fresh machine:

1. **DaVinci Resolve Studio** (the free version does not support scripting)
2. **Python 3** — [download from python.org](https://www.python.org/downloads/)
   and install with defaults. Without it Resolve hides `.py` scripts from
   the menu. Restart Resolve after installing.
3. **ffmpeg** (used for the audio analysis):
   - **Windows**: `winget install Gyan.FFmpeg` in PowerShell (winget is
     built into Windows 10/11), then restart Resolve
   - **macOS**: `brew install ffmpeg` — or, if you don't use
     [Homebrew](https://brew.sh), download a
     [static build](https://evermeet.cx/ffmpeg/) and move the `ffmpeg`
     file to `/usr/local/bin/`
   - **Linux**: `sudo apt install ffmpeg` (or your distro's equivalent)

The installer below checks both Python and ffmpeg and tells you exactly
what's missing.

## Install

**macOS / Linux** — run in a terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/taddak/podcast-multicam-editor/main/install.sh | bash
```

**Windows** — run in PowerShell:

```powershell
irm https://raw.githubusercontent.com/taddak/podcast-multicam-editor/main/install.ps1 | iex
```

> Tip: if you get `curl: (3) URL rejected`, your chat app replaced the
> straight quotes with typographic ones — use the quote-free commands above
> exactly as written.

Or manually: download `Podcast Multicam Editor.py` and drop it into Resolve's
scripts folder:

| OS | Folder |
|---|---|
| macOS | `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit/` |
| Windows | `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Edit\` |
| Linux | `~/.local/share/DaVinciResolve/Fusion/Scripts/Edit/` |

## Usage

1. Prepare a timeline: each camera on its own video track, each microphone on
   its own audio track, everything in sync. Keep it open.
2. Run **Workspace → Scripts → Edit → Podcast Multicam Editor**.
3. In the window:
   - name the speakers on their audio tracks (pre-filled as *Speaker 1, 2…*)
     — clear a name to ignore a track (music, ambience), give two tracks the
     same name to merge them into one speaker;
   - for every video track tick who is on camera — tick everyone for the
     wide shot;
   - tune the settings on top (sensitivity, minimum shot length, wide-shot
     behaviour).
4. Click **Create cut**. A new timeline `<name> - AutoCut` appears next to
   your original. Diagnostics are printed to **Workspace → Console**.

### Settings

Speech detection has no knobs — thresholds are computed automatically per
microphone and adapt to quiet and loud voices.

| Setting | Meaning |
|---|---|
| Min. shot length | no shot will be shorter than this |
| Ignore sounds under | coughs and short noises below this length don't cut |
| Wide shot on monologue | how often to break up long monologues with the wide |
| Min. wide shot | the wide never appears shorter than this |

## Troubleshooting

- **The script doesn't appear in the menu** — in order of likelihood:
  1. restart Resolve (the Scripts menu is scanned on startup);
  2. look in the submenu: Workspace → Scripts → **Edit**;
  3. install **Python 3** from [python.org](https://www.python.org/downloads/)
     and restart Resolve — without Python, Resolve silently hides all `.py`
     scripts from the menu;
  4. make sure you run DaVinci Resolve **Studio** — the free edition does
     not support the scripting API.
- **"Found no reason to cut anywhere"** — check that each speaker really has
  their own named audio track. The dialog shows per-speaker diagnostics
  (detected speech levels and talk time).
- **A speaker is reported as mic bleed** — their mic is much quieter than
  the others and almost only active when someone else talks; check the
  mapping or their recording level.
- **Resolve looks busy during analysis** — that's normal for long episodes;
  the progress window and the Console show what's happening.

## Development

Pure-logic tests (no Resolve needed):

```bash
python3 tests/logic_test.py
```

## License

[MIT](LICENSE) — free to use, modify and share.
