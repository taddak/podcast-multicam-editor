#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Podcast Multicam Editor for DaVinci Resolve
============================================
Automatically cuts a multicam podcast timeline based on who is speaking.

Usage:
  1. Prepare a timeline: each camera on its own video track,
     each microphone (speaker) on its own audio track, all in sync.
  2. Keep that timeline open and run this script:
     Workspace -> Scripts -> Edit -> Podcast Multicam Editor
  3. In the window, name the speakers on their audio tracks (clear the
     name to ignore a track, use the same name to merge two mics into
     one speaker) and tick who is on camera for each video track
     (tick everyone = wide shot).
  4. The script duplicates the timeline as "<name> - AutoCut" and builds
     the cut on the duplicate: every shot stays on its camera's own
     video track (checkerboard), audio tracks including all of your
     post-processing remain untouched.

The original timeline is never modified.

How detection works:
  - Loudness of every microphone is measured in 50 ms windows (dB).
  - A speech threshold is found per speaker with Otsu's method
    (splits the loudness histogram into "noise" and "speech"), which
    works even when somebody talks most of the time.
  - Crosstalk between microphones is suppressed by comparing tracks
    against each other: every track is normalized to its own speech
    level, and a speaker who is much quieter than the loudest one at
    that moment (mic bleed, backchannel) does not count.
  - The camera is chosen from the set of active speakers; overlaps with
    no matching camera go to the dominant (relatively loudest) speaker.
"""

import os
import re
import sys
import json
import math
import array
import shutil
import warnings
import threading
import subprocess

__version__ = "1.1.0"

# where releases live; the script checks this on start and can update itself
UPDATE_URL = ("https://raw.githubusercontent.com/taddak/"
              "podcast-multicam-editor/main/Podcast%20Multicam%20Editor.py")

# audioop must be imported here, in the main thread, with its deprecation
# warning suppressed: emitting a warning inside a worker thread writes to
# Resolve's console, which is not thread-safe and crashes the script
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    try:
        import audioop
    except ImportError:
        audioop = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HOP_S = 0.05          # audio analysis step (50 ms)
SAMPLE_RATE = 8000    # analysis sample rate (plenty for speech RMS)
ANALYSIS_WORKERS = 4  # how many clips to analyze concurrently
STABLE_S = 0.4        # how long a new speaker must talk before we cut
OVERLAP_STABLE_S = 1.2  # confidence needed before cutting to a multi-person
                        # shot; shorter overlaps are usually just crosstalk
WIDE_OVERLAP_S = 4.0  # overlap must last this long before the wide shot is
                      # justified; shorter overlaps cut to the other
                      # speaker's close-up (reaction shot) instead
GAP_FILL_S = 0.35     # shorter pauses within speech get merged
WIDE_SHOT_S = 4.0     # length of the wide shot inserted during monologues
CROSSTALK_DB = 8.0    # how many dB quieter counts as mic bleed
EARLY_CUT_S = 1.2     # cut into the pause this early before a new speaker
EARLY_CUT_TAIL_S = 0.3  # pause left on the outgoing speaker
SILENCE_DB = -100.0   # level where a speaker has no media
THRESHOLD_MIN = -55.0 # speech threshold never below this level
THRESHOLD_MAX = -12.0 # ...nor above this one

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
# keeps ffmpeg from flashing a console window on every clip (Windows)
SUBPROCESS_FLAGS = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW

if IS_WIN:
    FFMPEG_CANDIDATES = [
        "ffmpeg",
        r"C:\ffmpeg\bin\ffmpeg.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
        os.path.expandvars(r"%ProgramFiles%\ffmpeg\bin\ffmpeg.exe"),
    ]
    FFMPEG_HINT = ("Install it with: winget install Gyan.FFmpeg\n"
                   "(then restart DaVinci Resolve)")
elif IS_MAC:
    FFMPEG_CANDIDATES = [
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/usr/bin/ffmpeg",
        "ffmpeg",
    ]
    FFMPEG_HINT = "Install it with: brew install ffmpeg"
else:
    FFMPEG_CANDIDATES = ["ffmpeg", "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"]
    FFMPEG_HINT = "Install it with your package manager, e.g.: sudo apt install ffmpeg"


def user_config_path():
    if IS_WIN:
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    elif IS_MAC:
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME",
                              os.path.expanduser("~/.config"))
    return os.path.join(base, "PodcastMulticamEditor.json")


CONFIG_PATH = user_config_path()

WIDE_FREQ_PRESETS = {
    "Off": None,
    "Occasional (~60 s)": 60.0,
    "Medium (~30 s)": 30.0,
    "Frequent (~15 s)": 15.0,
}


# ---------------------------------------------------------------------------
# Resolve / Fusion UI connection
# ---------------------------------------------------------------------------

def get_resolve_env():
    g = globals()
    _resolve = g.get("resolve")
    if _resolve is None:
        import DaVinciResolveScript as dvr
        _resolve = dvr.scriptapp("Resolve")
    _fusion = g.get("fusion") or _resolve.Fusion()
    _bmd = g.get("bmd")
    if _bmd is None:
        import DaVinciResolveScript as _bmd
    return _resolve, _fusion, _bmd


RESOLVE, FUSION, BMD = get_resolve_env()
UI = FUSION.UIManager
DISP = BMD.UIDispatcher(UI)

PROGRESS = None  # ProgressWindow while processing, see below


def show_message(title, text):
    win = DISP.AddWindow(
        {"WindowTitle": title, "ID": "msgWin", "Geometry": [400, 300, 500, 200]},
        UI.VGroup([
            UI.Label({"Text": text, "WordWrap": True, "Weight": 1}),
            UI.HGroup({"Weight": 0}, [
                UI.HGap(0, 1),
                UI.Button({"ID": "msgOk", "Text": "OK", "Weight": 0}),
            ]),
        ]))
    win.On.msgOk.Clicked = lambda ev: DISP.ExitLoop()
    win.On.msgWin.Close = lambda ev: DISP.ExitLoop()
    win.Show()
    DISP.RunLoop()
    win.Hide()


def fail(text):
    global PROGRESS
    if PROGRESS:
        PROGRESS.close()
        PROGRESS = None
    show_message("Podcast Multicam Editor - error", text)
    sys.exit(0)


class ProgressWindow:
    """Small always-on window so Resolve does not look frozen while the
    script analyzes audio and builds the timeline."""

    def __init__(self):
        self.win = DISP.AddWindow(
            {"WindowTitle": "Podcast Multicam Editor", "ID": "progWin",
             "Geometry": [480, 380, 440, 120]},
            UI.VGroup({"Spacing": 6}, [
                UI.Label({"ID": "progText", "Text": "Processing…",
                          "Weight": 0}),
                UI.Label({"ID": "progBar", "Text": "", "Weight": 0,
                          "StyleSheet": "color: #E6A23C;"}),
                UI.Label({"Text": "Details in Workspace → Console.",
                          "StyleSheet": "color: #999999;", "Weight": 0}),
            ]))
        self.itm = self.win.GetItems()
        self.win.Show()

    def update(self, text, frac=None):
        try:
            self.itm["progText"].Text = text
            if frac is not None:
                filled = int(round(max(0.0, min(1.0, frac)) * 30))
                self.itm["progBar"].Text = "█" * filled + "░" * (30 - filled)
        except Exception:
            pass

    def close(self):
        try:
            self.win.Hide()
        except Exception:
            pass


def progress_update(text, frac=None):
    if PROGRESS:
        PROGRESS.update(text, frac)


# ---------------------------------------------------------------------------
# Self-update (checked quietly on start, applied only when the user agrees)
# ---------------------------------------------------------------------------

UPDATE_RESULT = {"content": None, "version": None}


def parse_version(text):
    m = re.search(r'__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', text)
    return tuple(int(g) for g in m.groups()) if m else None


def start_update_check():
    """Fetches the latest release in a background thread. The thread only
    stores the result - no console writes, no UI, no Resolve API."""
    if "__GITHUB_" "USER__" in UPDATE_URL:
        return  # development copy without a configured update source

    def worker():
        try:
            import urllib.request
            req = urllib.request.Request(
                UPDATE_URL, headers={"User-Agent": "PodcastMulticamEditor"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                text = resp.read().decode("utf-8")
            latest = parse_version(text)
            current = parse_version('__version__ = "{}"'.format(__version__))
            if (latest and current and latest > current
                    and "Podcast Multicam Editor" in text):
                UPDATE_RESULT["content"] = text
                UPDATE_RESULT["version"] = latest
        except Exception:
            pass  # offline or GitHub unreachable - never bother the user

    threading.Thread(target=worker, daemon=True).start()


def ask_yes_no(title, text, yes_label, no_label):
    win = DISP.AddWindow(
        {"WindowTitle": title, "ID": "ynWin", "Geometry": [420, 320, 480, 170]},
        UI.VGroup([
            UI.Label({"Text": text, "WordWrap": True, "Weight": 1}),
            UI.HGroup({"Weight": 0}, [
                UI.Button({"ID": "ynNo", "Text": no_label, "Weight": 0}),
                UI.HGap(0, 1),
                UI.Button({"ID": "ynYes", "Text": yes_label, "Weight": 0}),
            ]),
        ]))
    result = {"yes": False}

    def on_yes(ev):
        result["yes"] = True
        DISP.ExitLoop()

    win.On.ynYes.Clicked = on_yes
    win.On.ynNo.Clicked = lambda ev: DISP.ExitLoop()
    win.On.ynWin.Close = lambda ev: DISP.ExitLoop()
    win.Show()
    DISP.RunLoop()
    win.Hide()
    return result["yes"]


def maybe_offer_update():
    """If the background check found a newer version, offer to install it.
    The new version takes effect the next time the script is run."""
    content = UPDATE_RESULT.get("content")
    version = UPDATE_RESULT.get("version")
    if not content or not version:
        return
    UPDATE_RESULT["content"] = None  # offer only once per run
    label = ".".join(str(n) for n in version)
    if not ask_yes_no(
            "Podcast Multicam Editor - update",
            "Version {} is available (you have {}).\n"
            "Update now? It takes effect the next time you run the "
            "script.".format(label, __version__),
            "Update", "Later"):
        return
    try:
        path = os.path.abspath(__file__)
        try:
            shutil.copy2(path, path + ".bak")
        except Exception:
            pass
        tmp = path + ".new"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
        show_message("Podcast Multicam Editor",
                     "Updated to version {}. The new version loads the "
                     "next time you run the script.".format(label))
    except Exception as exc:
        show_message("Podcast Multicam Editor",
                     "Update failed ({}). Download it manually from:\n"
                     "{}".format(exc, UPDATE_URL))


# ---------------------------------------------------------------------------
# Reading the timeline
# ---------------------------------------------------------------------------

def find_ffmpeg():
    for cand in FFMPEG_CANDIDATES:
        if os.path.sep in cand:
            if os.path.exists(cand):
                return cand
        else:
            from shutil import which
            if which(cand):
                return cand
    return None


def collect_tracks(timeline, track_type):
    """Returns tracks with their clips: index, name, items."""
    tracks = []
    count = timeline.GetTrackCount(track_type)
    for idx in range(1, int(count) + 1):
        items = timeline.GetItemListInTrack(track_type, idx) or []
        infos = []
        for it in items:
            mpi = it.GetMediaPoolItem()
            if mpi is None:
                continue
            infos.append({
                "start": int(it.GetStart()),
                "end": int(it.GetEnd()),
                "leftOffset": int(it.GetLeftOffset()),
                "mpi": mpi,
                "path": mpi.GetClipProperty("File Path") or "",
                "name": mpi.GetName() or "",
            })
        infos.sort(key=lambda x: x["start"])
        name = timeline.GetTrackName(track_type, idx) or "{} {}".format(
            "Video" if track_type == "video" else "Audio", idx)
        tracks.append({"index": idx, "name": name, "items": infos})
    return tracks


# ---------------------------------------------------------------------------
# Saving / loading last used settings
# ---------------------------------------------------------------------------

def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main window - settings + track mapping laid out like the Resolve timeline
# ---------------------------------------------------------------------------

SPEAKER_COLORS = [
    "#E6A23C", "#5AA3E8", "#67C23A", "#F56C6C", "#B37FEB",
    "#F5D442", "#43C6C8", "#F49BC1", "#C58A5A", "#9AA5B1",
]

VIDEO_CLIP_CSS = ("background-color: #4E7DC1; color: #0A1830; "
                  "border-radius: 3px;")
AUDIO_CLIP_CSS = ("background-color: #5C9E60; color: #08240B; "
                  "border-radius: 3px;")
GAP_CSS = "background-color: #232323; border-radius: 2px;"
BADGE_V_CSS = ("background-color: #33507E; color: #D8E4F5; "
               "font-weight: bold; border-radius: 3px;")
BADGE_A_CSS = ("background-color: #2F5C33; color: #DCF0DD; "
               "font-weight: bold; border-radius: 3px;")
HINT_CSS = "color: #999999;"
CENTER = {"AlignHCenter": True, "AlignVCenter": True}

SETTING_FIELDS = [
    ("minShot", "Min. shot length (s)"),
    ("interject", "Ignore sounds under (s)"),
    ("wideFreq", "Wide shot on monologue"),
    ("wideMin", "Min. wide shot (s)"),
]


def setting_options():
    return {
        "minShot": ["1.0", "1.5", "2.0", "2.5", "3.0", "4.0"],
        "interject": ["0.2", "0.3", "0.4", "0.6", "1.0"],
        "wideFreq": list(WIDE_FREQ_PRESETS.keys()),
        "wideMin": ["2.0", "3.0", "4.0", "5.0"],
    }


def speaker_color(s):
    return SPEAKER_COLORS[s % len(SPEAKER_COLORS)]


def elide(text, n=16):
    return text if len(text) <= n else text[:n - 1] + "…"


def clip_bar(track, tl_start, tl_end, clip_css):
    """A strip of clips proportional in time - a mini preview of the track
    just like it looks in the timeline."""
    total = max(1, tl_end - tl_start)
    items = track["items"]
    parts = []
    if len(items) > 14:
        parts.append(("gap", items[0]["start"] - tl_start, ""))
        parts.append(("clip", items[-1]["end"] - items[0]["start"],
                      "{} clips".format(len(items))))
        parts.append(("gap", tl_end - items[-1]["end"], ""))
    else:
        pos = tl_start
        for it in items:
            parts.append(("gap", it["start"] - pos, ""))
            parts.append(("clip", it["end"] - it["start"], it["name"]))
            pos = it["end"]
        parts.append(("gap", tl_end - pos, ""))

    widgets = []
    for kind, dur, label in parts:
        w = max(0.0, dur / total)
        if kind == "gap":
            if w > 0.002:
                widgets.append(UI.Label({"Text": "", "Weight": w,
                                         "StyleSheet": GAP_CSS,
                                         "MinimumSize": [2, 20]}))
        else:
            text = elide(label) if w > 0.12 else ""
            widgets.append(UI.Label({"Text": text, "Weight": max(w, 0.03),
                                     "Alignment": dict(CENTER),
                                     "StyleSheet": clip_css,
                                     "MinimumSize": [6, 20]}))
    if not widgets:
        widgets = [UI.Label({"Text": "", "Weight": 1, "StyleSheet": GAP_CSS})]
    return UI.HGroup({"Weight": 1, "Spacing": 1}, widgets)


def show_main_window(audio_tracks, video_tracks, tl_start, tl_end, state):
    """One window with everything, laid out like the Resolve timeline:
    video tracks on top (V3, V2, V1), audio tracks below (A1, A2, ...).

    Speakers ARE the audio tracks: each audio row has a name field
    pre-filled with "Speaker N". Overwrite it with a real name, clear it
    to ignore the track, or give two tracks the same name to merge them
    into one speaker.

    Returns "ok" / "cancel"; current values are stored into state.
    """
    opts = setting_options()
    n_spk = len(audio_tracks)
    names = state["names"]

    def default_name(s):
        return "Speaker {}".format(s + 1)

    def initial_name(s):
        if s < len(names) and names[s] is not None and str(names[s]).strip():
            return str(names[s])
        if s < len(names) and names[s] == "":
            return ""  # the user cleared this track on purpose last time
        return default_name(s)

    def badge(text, css):
        return UI.Label({"Text": text, "StyleSheet": css,
                         "Alignment": dict(CENTER), "Weight": 0,
                         "MinimumSize": [34, 20], "MaximumSize": [34, 22]})

    def setting_cell(cid, label):
        return UI.VGroup({"Weight": 1, "Spacing": 1}, [
            UI.Label({"Text": label, "StyleSheet": HINT_CSS, "Weight": 0}),
            UI.ComboBox({"ID": cid, "Weight": 0}),
        ])

    settings_rows = [
        UI.HGroup({"Weight": 0, "Spacing": 10},
                  [setting_cell(cid, lbl) for cid, lbl in SETTING_FIELDS[:2]]),
        UI.HGroup({"Weight": 0, "Spacing": 10},
                  [setting_cell(cid, lbl) for cid, lbl in SETTING_FIELDS[2:]]),
    ]

    # video tracks top-down, exactly like the timeline (V3, V2, V1)
    video_sorted = sorted(video_tracks, key=lambda t: -t["index"])
    video_rows = []
    for tr in video_sorted:
        checks = [UI.CheckBox({
            "ID": "vid{}s{}".format(tr["index"], s),
            "Text": elide(initial_name(s) or "—", 12),
            "Weight": 0,
            "StyleSheet": "QCheckBox {{ color: {}; }}".format(
                speaker_color(s)),
        }) for s in range(n_spk)]
        video_rows.append(UI.HGroup(
            {"Weight": 0, "Spacing": 6},
            [badge("V{}".format(tr["index"]), BADGE_V_CSS),
             clip_bar(tr, tl_start, tl_end, VIDEO_CLIP_CSS)] +
            checks +
            [UI.Label({"ID": "role{}".format(tr["index"]), "Text": "",
                       "Weight": 0, "StyleSheet": "color: #AAAAAA;",
                       "MinimumSize": [104, 18]})]))

    audio_rows = []
    for pos, tr in enumerate(audio_tracks):
        audio_rows.append(UI.HGroup(
            {"Weight": 0, "Spacing": 6},
            [badge("A{}".format(tr["index"]), BADGE_A_CSS),
             clip_bar(tr, tl_start, tl_end, AUDIO_CLIP_CSS),
             UI.Label({"Text": "●", "Weight": 0,
                       "StyleSheet": "color: {}; font-size: 15px;".format(
                           speaker_color(pos))}),
             UI.LineEdit({"ID": "name{}".format(pos),
                          "Text": initial_name(pos),
                          "PlaceholderText": "(unused)",
                          "Weight": 0,
                          "MinimumSize": [140, 22],
                          "MaximumSize": [170, 24]})]))

    separator = UI.Label({"Text": "", "Weight": 0,
                          "StyleSheet": "background-color: #3A3A3A;",
                          "MinimumSize": [10, 2], "MaximumSize": [9999, 2]})

    n_rows = len(video_rows) + len(audio_rows)
    height = min(280 + n_rows * 30, 780)
    width = max(920, 620 + n_spk * 66)

    win = DISP.AddWindow(
        {"WindowTitle": "Podcast Multicam Editor v" + __version__,
         "ID": "mainWin", "Geometry": [220, 120, width, height]},
        UI.VGroup(
            {"Spacing": 6},
            settings_rows +
            [UI.VGap(4, 0),
             UI.Label({"Text": "CAMERAS - tick who is on camera "
                               "(everyone = wide shot)",
                       "StyleSheet": HINT_CSS, "Weight": 0})] +
            video_rows +
            [separator,
             UI.Label({"Text": "MICROPHONES - name the speaker on each "
                               "track (clear = ignore, same name = same "
                               "speaker)",
                       "StyleSheet": HINT_CSS, "Weight": 0})] +
            audio_rows +
            [UI.VGap(0, 1),
             UI.HGroup({"Weight": 0}, [
                 UI.Button({"ID": "cancelBtn", "Text": "Cancel", "Weight": 0}),
                 UI.HGap(0, 1),
                 UI.Button({"ID": "runBtn", "Text": "Create cut",
                            "Weight": 0}),
             ])]))

    itm = win.GetItems()

    for cid, _ in SETTING_FIELDS:
        for o in opts[cid]:
            itm[cid].AddItem(o)
        val = state["settings"][cid]
        itm[cid].CurrentIndex = opts[cid].index(val) if val in opts[cid] else 0

    for pos, tr in enumerate(video_tracks):
        sel = state["videoSel"].get(tr["index"])
        if not isinstance(sel, list):
            sel = [pos] if pos < n_spk else list(range(n_spk))
        for s in range(n_spk):
            itm["vid{}s{}".format(tr["index"], s)].Checked = s in sel

    def live_names():
        return [itm["name{}".format(s)].Text for s in range(n_spk)]

    def refresh_roles(ev=None):
        current = live_names()
        used = [s for s in range(n_spk) if current[s].strip()]
        for tr in video_tracks:
            sel = [s for s in used
                   if itm["vid{}s{}".format(tr["index"], s)].Checked]
            if not sel:
                txt = "unused"
            elif len(set(current[s].strip().lower() for s in used)) == \
                    len(set(current[s].strip().lower() for s in sel)) \
                    and len(sel) == len(used):
                txt = "WIDE"
            elif len(sel) == 1:
                txt = "solo · " + current[sel[0]].strip()
            else:
                txt = "group ({})".format(len(sel))
            try:
                itm["role{}".format(tr["index"])].Text = txt
            except Exception:
                pass

    def refresh_names(ev=None):
        current = live_names()
        for s in range(n_spk):
            label = elide(current[s].strip() or "—", 12)
            enabled = bool(current[s].strip())
            for tr in video_tracks:
                try:
                    box = itm["vid{}s{}".format(tr["index"], s)]
                    box.Text = label
                    box.Enabled = enabled
                except Exception:
                    pass
        refresh_roles()

    refresh_names()

    result = {"action": "cancel"}

    def collect_state():
        state["settings"] = {cid: opts[cid][itm[cid].CurrentIndex]
                             for cid, _ in SETTING_FIELDS}
        state["names"] = live_names()
        state["videoSel"] = {
            tr["index"]: [s for s in range(n_spk)
                          if itm["vid{}s{}".format(tr["index"], s)].Checked]
            for tr in video_tracks}

    def on_run(ev):
        collect_state()
        result["action"] = "ok"
        DISP.ExitLoop()

    win.On.runBtn.Clicked = on_run
    win.On.cancelBtn.Clicked = lambda ev: DISP.ExitLoop()
    win.On.mainWin.Close = lambda ev: DISP.ExitLoop()
    for s in range(n_spk):
        win.On["name{}".format(s)].TextChanged = refresh_names
    for tr in video_tracks:
        for s in range(n_spk):
            win.On["vid{}s{}".format(tr["index"], s)].Clicked = refresh_roles

    win.Show()
    DISP.RunLoop()
    win.Hide()
    return result["action"]


# ---------------------------------------------------------------------------
# Loudness measurement
# ---------------------------------------------------------------------------

def compute_rms_windows(pcm_bytes, hop_samples):
    """RMS per window; returns values 0..32768.

    No imports and no console writes here - this runs in worker threads.
    """
    if audioop is not None:
        rms_fn = lambda buf: audioop.rms(buf, 2)
    else:
        def rms_fn(buf):
            arr = array.array("h")
            arr.frombytes(buf)
            if not arr:
                return 0
            acc = 0
            for v in arr:
                acc += v * v
            return int(math.sqrt(acc / len(arr)))

    hop_bytes = hop_samples * 2
    out = []
    for pos in range(0, len(pcm_bytes) - hop_bytes + 1, hop_bytes):
        out.append(rms_fn(pcm_bytes[pos:pos + hop_bytes]))
    return out


def rms_to_db(rms):
    return 20.0 * math.log10(max(rms, 1) / 32768.0)


def measure_levels_in_item(ffmpeg, item, fps):
    """Extracts the clip's audio and returns (dB values per window, error).

    Runs in worker threads: must not print or import anything - writing
    to Resolve's console from a thread crashes the script.
    """
    src_start_s = item["leftOffset"] / fps
    dur_s = (item["end"] - item["start"]) / fps
    if dur_s <= 0 or not item["path"]:
        return [], None

    cmd = [ffmpeg, "-v", "error",
           "-ss", "{:.3f}".format(src_start_s),
           "-t", "{:.3f}".format(dur_s),
           "-i", item["path"],
           "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
           "-f", "s16le", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True,
                              creationflags=SUBPROCESS_FLAGS)
    except Exception as exc:
        return [], "ffmpeg failed for {}: {}".format(item["name"], exc)
    if proc.returncode != 0 or not proc.stdout:
        return [], "ffmpeg failed for {}: {}".format(
            item["name"], proc.stderr.decode("utf-8", "replace")[:200])

    hop_samples = int(SAMPLE_RATE * HOP_S)
    return [rms_to_db(r)
            for r in compute_rms_windows(proc.stdout, hop_samples)], None


def build_speaker_levels(ffmpeg, audio_tracks, audio_map, n_speakers,
                         fps, tl_start, total_hops):
    """Loudness (dB) over the whole timeline for every speaker.

    Clips are analyzed concurrently (ffmpeg decodes outside Python), so
    even 6 microphones x a full episode takes a fraction of real time.
    """
    from concurrent.futures import ThreadPoolExecutor

    levels = [[SILENCE_DB] * total_hops for _ in range(n_speakers)]
    tasks = []
    for tr in audio_tracks:
        speaker = audio_map.get(tr["index"], 0)
        if speaker <= 0:
            continue
        for item in tr["items"]:
            tasks.append((speaker - 1, tr["index"], item))
    if not tasks:
        return levels

    print("Analyzing {} audio clips ({} in parallel)...".format(
        len(tasks), min(ANALYSIS_WORKERS, len(tasks))))

    def work(task):
        s_idx, t_idx, item = task
        db, err = measure_levels_in_item(ffmpeg, item, fps)
        return s_idx, t_idx, item, db, err

    done = 0
    with ThreadPoolExecutor(max_workers=ANALYSIS_WORKERS) as ex:
        # all printing happens here in the main thread, never in workers
        for s_idx, t_idx, item, db, err in ex.map(work, tasks):
            done += 1
            print("  [{}/{}] A{} {}".format(done, len(tasks), t_idx,
                                            item["name"]))
            if err:
                print("  ! " + err)
            progress_update("Analyzing audio {}/{}: {}".format(
                done, len(tasks), elide(item["name"], 28)),
                done / len(tasks))
            offset_hop = int(round((item["start"] - tl_start) / fps / HOP_S))
            for k, val in enumerate(db):
                pos = offset_hop + k
                if 0 <= pos < total_hops and val > levels[s_idx][pos]:
                    levels[s_idx][pos] = val
    return levels


# ---------------------------------------------------------------------------
# Speech detection: automatic threshold + crosstalk suppression
# ---------------------------------------------------------------------------

def percentile(sorted_vals, p):
    if not sorted_vals:
        return SILENCE_DB
    k = max(0, min(len(sorted_vals) - 1,
                   int(round(p / 100.0 * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def otsu_threshold(values):
    """Finds the split between "noise" and "speech" in a dB histogram.

    Unlike a percentile-based noise estimate this makes no assumption
    about how much of the time is silence, so it also works for tracks
    where somebody talks most of the time.
    """
    lo, hi, step = -80.0, 0.0, 1.0
    nbins = int((hi - lo) / step)
    hist = [0] * nbins
    for v in values:
        b = int((min(max(v, lo), hi - 0.001) - lo) / step)
        hist[b] += 1
    total = sum(hist)
    if total == 0:
        return None

    sum_all = sum(i * h for i, h in enumerate(hist))
    w_bg = 0
    sum_bg = 0.0
    best_var = -1.0
    best_bin = None
    for i in range(nbins):
        w_bg += hist[i]
        if w_bg == 0:
            continue
        w_fg = total - w_bg
        if w_fg == 0:
            break
        sum_bg += i * hist[i]
        mean_bg = sum_bg / w_bg
        mean_fg = (sum_all - sum_bg) / w_fg
        var_between = w_bg * w_fg * (mean_bg - mean_fg) ** 2
        if var_between > best_var:
            best_var = var_between
            best_bin = i
    if best_bin is None:
        return None

    # separation check: how far apart the two class means are
    w_bg = sum(hist[:best_bin + 1])
    sum_bg = sum(i * h for i, h in enumerate(hist[:best_bin + 1]))
    mean_bg = lo + (sum_bg / w_bg) * step if w_bg else lo
    w_fg = total - w_bg
    mean_fg = lo + ((sum_all - sum_bg) / w_fg) * step if w_fg else hi
    return lo + (best_bin + 1) * step, mean_fg - mean_bg


def fill_gaps(flags, max_gap):
    """Merges short pauses (False) inside speech."""
    out = list(flags)
    i = 0
    n = len(out)
    while i < n:
        if not out[i]:
            j = i
            while j < n and not out[j]:
                j += 1
            if 0 < i and j < n and (j - i) <= max_gap:
                for k in range(i, j):
                    out[k] = True
            i = j
        else:
            i += 1
    return out


def drop_short_runs(flags, min_len):
    """Drops short bursts of speech (interjections)."""
    out = list(flags)
    i = 0
    n = len(out)
    while i < n:
        if out[i]:
            j = i
            while j < n and out[j]:
                j += 1
            if (j - i) < min_len:
                for k in range(i, j):
                    out[k] = False
            i = j
        else:
            i += 1
    return out


def smooth_levels(vals, window=7):
    """Moving average - smooths speech fluctuation (syllables, breaths)
    so comparing tracks against each other does not flicker."""
    n = len(vals)
    if n == 0 or window <= 1:
        return list(vals)
    half = window // 2
    out = [0.0] * n
    acc = sum(vals[:half + 1])
    cnt = half + 1
    for i in range(n):
        out[i] = acc / cnt
        add = i + half + 1
        if add < n:
            acc += vals[add]
            cnt += 1
        rem = i - half
        if rem >= 0:
            acc -= vals[rem]
            cnt -= 1
    return out


def compute_activity(levels, interject_s, names=None):
    """Determines who speaks when, and who is dominant.

    Thresholds are fully automatic (per-mic Otsu with bleed re-split),
    so there is no sensitivity knob - the analysis adapts to quiet and
    loud microphones by itself.

    Returns (activity, dominant, stats):
      activity[s][h] - speaker s talks in window h
      dominant[h]    - index of the relatively loudest speaker, else -1
      stats[s]       - diagnostic text
    """
    n_speakers = len(levels)
    total_hops = len(levels[0]) if levels else 0
    levels = [smooth_levels(lv) for lv in levels]

    def spk(s):
        if names and s < len(names):
            return names[s]
        return "Speaker {}".format(s + 1)

    thresholds = []
    speech_refs = []
    stats = []
    for s in range(n_speakers):
        valid = sorted(v for v in levels[s] if v > SILENCE_DB + 1)
        if not valid or percentile(valid, 95) < -48:
            # this speaker is silent the whole time (or track missing)
            thresholds.append(None)
            speech_refs.append(None)
            stats.append("{}: no speech found".format(spk(s)))
            continue
        p95 = percentile(valid, 95)
        res = otsu_threshold(valid)
        if res is None:
            thr = percentile(valid, 50)
        else:
            thr, separation = res
            if separation < 6.0:
                # unimodal track (talks practically all the time) - Otsu
                # would slice through the speech itself
                thr = p95 - 20.0
            elif p95 - thr > 18.0:
                # threshold suspiciously deep below the speech level: the
                # histogram probably has three modes (noise / mic bleed /
                # speech) and Otsu split at the noise - on a quiet mic the
                # bleed then sits above the threshold and fakes overlaps.
                # Split the upper part again to separate bleed from speech.
                upper = [v for v in valid if v > thr]
                res2 = otsu_threshold(upper)
                if res2 is not None and res2[1] >= 8.0:
                    thr = res2[0]
        # keep the threshold reasonably close below the speech level -
        # Otsu can slide down to the noise on multimodal distributions
        thr = min(max(thr, p95 - 28.0, THRESHOLD_MIN), THRESHOLD_MAX)
        speech = [v for v in valid if v > thr]
        ref = percentile(sorted(speech), 90) if speech else percentile(valid, 95)
        thresholds.append(thr)
        speech_refs.append(ref)
        stats.append(
            "{}: threshold {:.0f} dB, speech level {:.0f} dB".format(
                spk(s), thr, ref))

    # raw activity + crosstalk suppression via normalized level comparison
    activity = [[False] * total_hops for _ in range(n_speakers)]
    for h in range(total_hops):
        active = [s for s in range(n_speakers)
                  if thresholds[s] is not None and levels[s][h] > thresholds[s]]
        if len(active) > 1:
            norms = {s: levels[s][h] - speech_refs[s] for s in active}
            top = max(norms.values())
            active = [s for s in active if norms[s] >= top - CROSSTALK_DB]
        for s in active:
            activity[s][h] = True

    gap_hops = max(1, int(round(GAP_FILL_S / HOP_S)))
    min_hops = max(1, int(round(interject_s / HOP_S)))
    for s in range(n_speakers):
        # first drop isolated blips (crosstalk, noises), then merge
        # pauses within speech, finally drop short interjections
        act = drop_short_runs(activity[s], 3)
        act = fill_gaps(act, gap_hops)
        activity[s] = drop_short_runs(act, min_hops)

    # a track whose activity almost entirely overlaps a much louder
    # speaker is just mic bleed (silent guest / wrong mapping) - drop it
    for s in range(n_speakers):
        if speech_refs[s] is None:
            continue
        act_s = sum(activity[s])
        if act_s == 0:
            continue
        for t in range(n_speakers):
            if t == s or speech_refs[t] is None:
                continue
            if speech_refs[s] >= speech_refs[t] - 6.0:
                continue
            overlap = sum(1 for h in range(total_hops)
                          if activity[s][h] and activity[t][h])
            if overlap / act_s > 0.9:
                activity[s] = [False] * total_hops
                stats[s] = ("{}: track looks like mic bleed of ({}) - "
                            "dropped (check the mapping)"
                            .format(spk(s), spk(t)))
                speech_refs[s] = None
                break

    dominant = [-1] * total_hops
    for h in range(total_hops):
        best = None
        best_norm = None
        for s in range(n_speakers):
            if not activity[s][h] or speech_refs[s] is None:
                continue
            norm = levels[s][h] - speech_refs[s]
            if best is None or norm > best_norm:
                best, best_norm = s, norm
        if best is not None:
            dominant[h] = best

    for s in range(n_speakers):
        if speech_refs[s] is not None:
            pct = 100.0 * sum(activity[s]) / max(total_hops, 1)
            stats[s] += ", talks {:.0f} % of the time".format(pct)

    return activity, dominant, stats


# ---------------------------------------------------------------------------
# Cut decision logic
# ---------------------------------------------------------------------------

def choose_camera(active_set, dominant, cameras, current):
    """Picks the best camera for the given set of active speakers.

    Order: exact match -> smallest superset -> solo camera of the
    dominant speaker -> None (hold the current one). The current camera
    wins ties to avoid needless switching.
    """
    if not active_set:
        return None

    def prefer_current(cands):
        if current is not None:
            for c in cands:
                if c["trackIndex"] == current["trackIndex"]:
                    return c
        return cands[0]

    exact = [c for c in cameras if c["speakers"] == active_set]
    if exact:
        return prefer_current(exact)

    supersets = sorted((c for c in cameras if active_set <= c["speakers"]),
                       key=lambda c: len(c["speakers"]))
    if supersets:
        smallest = [c for c in supersets
                    if len(c["speakers"]) == len(supersets[0]["speakers"])]
        return prefer_current(smallest)

    if dominant >= 0:
        dom_cams = sorted((c for c in cameras if dominant in c["speakers"]),
                          key=lambda c: len(c["speakers"]))
        if dom_cams:
            smallest = [c for c in dom_cams
                        if len(c["speakers"]) == len(dom_cams[0]["speakers"])]
            return prefer_current(smallest)
    return None


def reaction_camera(active_set, dominant, cameras, current):
    """Solo close-up of an active speaker who is not on screen - used
    instead of the wide shot for short overlaps (a reaction shot)."""
    on_screen = current["speakers"] if current else frozenset()
    candidates = sorted((s for s in active_set if s not in on_screen),
                        key=lambda s: 0 if s == dominant else 1)
    for s in candidates:
        for c in cameras:
            if c["speakers"] == frozenset({s}):
                return c
    return None


def plan_shots(activity, dominant, cameras, wide_cam, total_hops,
               min_shot_s, wide_every_s, wide_min_s=3.0):
    """Returns the list of shots (startHop, endHop, camera)."""
    min_shot = max(1, int(round(min_shot_s / HOP_S)))
    wide_min = max(min_shot, int(round(wide_min_s / HOP_S)))
    stable = max(1, int(round(STABLE_S / HOP_S)))
    stable_multi = max(stable, int(round(OVERLAP_STABLE_S / HOP_S)))
    n_speakers = len(activity)

    def active_at(h):
        return frozenset(s for s in range(n_speakers) if activity[s][h])

    # starting camera: the first one where somebody talks, else wide/first
    current = None
    for h in range(total_hops):
        cam = choose_camera(active_at(h), dominant[h], cameras, None)
        if cam:
            current = cam
            break
    if current is None:
        current = wide_cam or cameras[0]

    shots = []
    last_cut = 0
    cand = None
    cand_since = 0
    cand_gap_start = 0
    cand_multi = False
    last_any = -1  # last window where anybody spoke at all
    multi_age = 0  # how long the current overlap has lasted
    early_hops = max(0, int(round(EARLY_CUT_S / HOP_S)))
    tail_hops = max(0, int(round(EARLY_CUT_TAIL_S / HOP_S)))
    wide_overlap = max(1, int(round(WIDE_OVERLAP_S / HOP_S)))

    for h in range(total_hops):
        act = active_at(h)
        multi_age = multi_age + 1 if len(act) > 1 else 0
        desired = choose_camera(act, dominant[h], cameras, current)
        is_reaction = False
        if (desired is not None and wide_cam is not None
                and desired["trackIndex"] == wide_cam["trackIndex"]
                and current["trackIndex"] != wide_cam["trackIndex"]
                and multi_age < wide_overlap):
            # young overlap: instead of the wide shot, cut to the other
            # speaker's close-up (reaction); the wide is only justified
            # once the overlap lasts
            alt = reaction_camera(act, dominant[h], cameras, current)
            if alt is not None:
                desired = alt
                is_reaction = True
        if desired is None or desired["trackIndex"] == current["trackIndex"]:
            cand = None
        else:
            if cand is None or desired["trackIndex"] != cand["trackIndex"]:
                cand = desired
                cand_since = h
                cand_gap_start = last_any + 1
                cand_multi = len(cand["speakers"]) > 1 or is_reaction
            # cutting because of an overlap needs more confidence -
            # a short overlap is usually just crosstalk or backchannel
            need_stable = stable_multi if cand_multi else stable
            if (h - cand_since + 1) >= need_stable:
                cut_base = cand_since
                if cand_gap_start < cand_since:
                    # there was a pause before the new speaker - cut into
                    # it so the camera is on them before they talk
                    cut_base = min(cand_since,
                                   max(cand_gap_start + tail_hops,
                                       cand_since - early_hops))
                # never leave a wide/group shot earlier than wide_min
                hold = (wide_min if len(current["speakers"]) > 1
                        else min_shot)
                cut_at = max(cut_base, last_cut + hold)
                if cut_at <= h:
                    shots.append((last_cut, cut_at, current))
                    current = cand
                    last_cut = cut_at
                    cand = None
        if act:
            last_any = h
    shots.append((last_cut, total_hops, current))

    # insert wide shots into long monologues
    if wide_every_s and wide_cam:
        wide_every = int(round(wide_every_s / HOP_S))
        wide_len = max(int(round(WIDE_SHOT_S / HOP_S)), wide_min)
        expanded = []
        for start, end, cam in shots:
            if cam["trackIndex"] == wide_cam["trackIndex"]:
                expanded.append((start, end, cam))
                continue
            pos = start
            while (end - pos) > wide_every + wide_len + min_shot:
                expanded.append((pos, pos + wide_every, cam))
                expanded.append((pos + wide_every,
                                 pos + wide_every + wide_len, wide_cam))
                pos += wide_every + wide_len
            expanded.append((pos, end, cam))
        shots = expanded

    # merge neighbouring shots on the same camera
    merged = []
    for start, end, cam in shots:
        if end <= start:
            continue
        if merged and merged[-1][2]["trackIndex"] == cam["trackIndex"]:
            merged[-1] = (merged[-1][0], end, merged[-1][2])
        else:
            merged.append((start, end, cam))
    return merged


# ---------------------------------------------------------------------------
# Building the new timeline
# ---------------------------------------------------------------------------

def hop_to_frame(h, fps):
    return int(round(h * HOP_S * fps))


def find_item_at(track, abs_frame):
    for item in track["items"]:
        if item["start"] <= abs_frame < item["end"]:
            return item
    return None


def count_items(tl, track_type):
    total = 0
    for i in range(1, int(tl.GetTrackCount(track_type)) + 1):
        total += len(tl.GetItemListInTrack(track_type, i) or [])
    return total


def calibrate_append(media_pool, new_tl, probe_item, new_start):
    """Measures the actual behaviour of AppendToTimeline with a probe clip.

    Returns (end_inclusive, record_offset):
      end_inclusive - True if endFrame means the last used frame
                      (otherwise it is the frame after the end)
      record_offset - how much Resolve shifts recordFrame against the
                      absolute timeline position
    The probe clip is deleted right away. Returns (False, 0) on failure.
    """
    probe_len = 50
    src_in = probe_item["leftOffset"]
    src_out = src_in + probe_len
    if probe_item["end"] - probe_item["start"] < probe_len + 2:
        return False, 0
    media_pool.AppendToTimeline([{
        "mediaPoolItem": probe_item["mpi"],
        "startFrame": int(src_in),
        "endFrame": int(src_out),
        "mediaType": 1,
        "trackIndex": 1,
        "recordFrame": int(new_start + 100),
    }])
    placed = new_tl.GetItemListInTrack("video", 1) or []
    if not placed:
        print("! Calibration failed, using default behaviour.")
        return False, 0
    it = placed[0]
    dur = int(it.GetEnd()) - int(it.GetStart())
    end_inclusive = (dur == probe_len + 1)
    record_offset = int(it.GetStart()) - (new_start + 100)
    try:
        new_tl.DeleteClips(placed)
    except Exception:
        pass
    leftover = new_tl.GetItemListInTrack("video", 1) or []
    if leftover:
        print("! Could not delete the probe clip - remove the first clip "
              "manually.")
    print("Calibration: endFrame {}, recordFrame offset {} frames.".format(
        "inclusive" if end_inclusive else "exclusive", record_offset))
    return end_inclusive, record_offset


def build_timeline(project, media_pool, old_tl, shots, cameras, wide_cam,
                   video_tracks, fps, tl_start):
    """Duplicates the original timeline (audio incl. all post-processing
    stays untouched), wipes the video on the duplicate and builds the cut
    there - every shot on its camera's own track (checkerboard, AutoPod
    style)."""
    base_name = old_tl.GetName() + " - AutoCut"
    name = base_name
    n = 2
    existing = {project.GetTimelineByIndex(i + 1).GetName()
                for i in range(int(project.GetTimelineCount()))}
    while name in existing:
        name = "{} {}".format(base_name, n)
        n += 1

    progress_update("Duplicating the timeline…")
    audio_before = count_items(old_tl, "audio")
    new_tl = None
    try:
        new_tl = old_tl.DuplicateTimeline(name)
    except Exception:
        new_tl = None
    if not new_tl:
        fail("Could not duplicate the timeline - that is needed to keep "
             "your audio post-processing intact.")
    project.SetCurrentTimeline(new_tl)
    new_start = int(new_tl.GetStartFrame())

    # wipe the video on the duplicate; audio tracks stay untouched
    victims = []
    for i in range(1, int(new_tl.GetTrackCount("video")) + 1):
        victims.extend(new_tl.GetItemListInTrack("video", i) or [])
    if victims:
        try:
            # unlink A/V so deleting video does not take audio with it
            new_tl.SetClipsLinked(victims, False)
        except Exception:
            pass
        new_tl.DeleteClips(victims)

    leftover = count_items(new_tl, "video")
    audio_after = count_items(new_tl, "audio")
    if leftover or audio_after < audio_before:
        # something went wrong - drop the duplicate, original is untouched
        problem = ("could not clear the video tracks" if leftover
                   else "deleting video took audio clips with it")
        media_pool.DeleteTimelines([new_tl])
        project.SetCurrentTimeline(old_tl)
        fail("Editing the duplicate failed ({}) - nothing was changed. "
             "Let me know and we will sort it out.".format(problem))

    track_by_index = {t["index"]: t for t in video_tracks}
    cam_priority = [c for c in cameras]

    # calibrate the exact AppendToTimeline behaviour (varies by version)
    probe_item = max((it for tr in video_tracks for it in tr["items"]),
                     key=lambda it: it["end"] - it["start"])
    end_inclusive, record_offset = calibrate_append(
        media_pool, new_tl, probe_item, new_start)
    end_fix = -1 if end_inclusive else 0

    # video shots - each one goes to its camera's own track
    clip_infos = []
    skipped = 0
    for start_hop, end_hop, cam in shots:
        f0 = tl_start + hop_to_frame(start_hop, fps)
        f1 = tl_start + hop_to_frame(end_hop, fps)
        if f1 <= f0:
            continue
        # find the clip on the chosen camera, or fall back to another one
        candidates = [cam] + ([wide_cam] if wide_cam else []) + cam_priority
        chosen = None
        for c in candidates:
            item = find_item_at(track_by_index[c["trackIndex"]], f0)
            if item:
                chosen = (c, item)
                break
        if not chosen:
            skipped += 1
            continue
        c, item = chosen
        src_in = item["leftOffset"] + (f0 - item["start"])
        src_out = item["leftOffset"] + (min(f1, item["end"]) - item["start"])
        clip_infos.append({
            "mediaPoolItem": item["mpi"],
            "startFrame": int(max(src_in, 0)),
            "endFrame": int(src_out) + end_fix,
            "mediaType": 1,
            "trackIndex": int(c["trackIndex"]),
            "recordFrame": int(new_start + (f0 - tl_start)) - record_offset,
        })

    print("Placing {} video shots...".format(len(clip_infos)))
    for i in range(0, len(clip_infos), 50):
        media_pool.AppendToTimeline(clip_infos[i:i + 50])
        progress_update("Placing video shots {}/{}…".format(
            min(i + 50, len(clip_infos)), len(clip_infos)),
            min(i + 50, len(clip_infos)) / max(1, len(clip_infos)))

    placed = count_items(new_tl, "video")
    if placed < len(clip_infos):
        print("! Only {} of {} shots were placed.".format(
            placed, len(clip_infos)))

    return new_tl, len(clip_infos), skipped


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global PROGRESS

    project = RESOLVE.GetProjectManager().GetCurrentProject()
    if not project:
        fail("No project is open.")
    timeline = project.GetCurrentTimeline()
    if not timeline:
        fail("No timeline is open. Open your synced multicam timeline "
             "and run the script again.")

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        fail("ffmpeg not found - it is needed for the audio analysis.\n"
             + FFMPEG_HINT)

    fps_str = timeline.GetSetting("timelineFrameRate") or \
        project.GetSetting("timelineFrameRate")
    fps = float(fps_str)

    audio_tracks = [t for t in collect_tracks(timeline, "audio") if t["items"]]
    video_tracks = [t for t in collect_tracks(timeline, "video") if t["items"]]

    if len(video_tracks) < 2:
        fail("The timeline needs at least 2 video tracks with clips "
             "(each camera on its own track).")
    if len(audio_tracks) < 2:
        fail("The timeline needs at least 2 audio tracks with clips "
             "(each speaker on their own microphone).")

    missing = [it["name"] for tr in audio_tracks for it in tr["items"]
               if not it["path"]]
    if missing:
        fail("Could not resolve the file path of these clips: {}. "
             "Multicam/compound clips are not supported - use a synced "
             "timeline with the original clips.".format(
                 ", ".join(missing[:5])))

    saved = load_config()
    # track mapping is remembered separately for every project
    maps_key = project.GetName() or "default"
    saved_maps = saved.get("mappings", {}).get(maps_key) or {}

    tl_start = int(timeline.GetStartFrame())
    tl_end = int(timeline.GetEndFrame())

    start_update_check()

    opts = setting_options()

    def valid(cid, val, fallback):
        return val if val in opts[cid] else fallback

    def int_keys(d):
        out = {}
        for k, v in (d or {}).items():
            try:
                out[int(k)] = v
            except (TypeError, ValueError):
                pass
        return out

    state = {
        "settings": {
            "minShot": valid("minShot", saved.get("minShot", ""), "2.0"),
            "interject": valid("interject", saved.get("interject", ""), "0.3"),
            "wideFreq": valid("wideFreq", saved.get("wideFreq", ""),
                              "Medium (~30 s)"),
            "wideMin": valid("wideMin", saved.get("wideMin", ""), "3.0"),
        },
        "names": list(saved_maps.get("names", [])),
        "videoSel": int_keys(saved_maps.get("videoMap")),
    }

    if show_main_window(audio_tracks, video_tracks,
                        tl_start, tl_end, state) != "ok":
        maybe_offer_update()
        return

    settings = state["settings"]

    # speakers are the named audio tracks; the same name merges tracks
    names_raw = [(state["names"][p] if p < len(state["names"]) else "")
                 for p in range(len(audio_tracks))]
    groups = {}         # normalized name -> speaker index
    speaker_names = []  # speaker index -> display name
    audio_map = {}      # track index -> speaker index + 1 (0 = unused)
    track_group = {}    # track position -> speaker index
    for pos, tr in enumerate(audio_tracks):
        nm = (names_raw[pos] or "").strip()
        if not nm:
            audio_map[tr["index"]] = 0
            continue
        key = nm.lower()
        if key not in groups:
            groups[key] = len(speaker_names)
            speaker_names.append(nm)
        audio_map[tr["index"]] = groups[key] + 1
        track_group[pos] = groups[key]

    n_speakers = len(speaker_names)
    if n_speakers < 2:
        fail("At least 2 speakers are needed - name at least two audio "
             "tracks.")

    video_map = {}
    for tr in video_tracks:
        sel = state["videoSel"].get(tr["index"], [])
        video_map[tr["index"]] = sorted({track_group[p] for p in sel
                                         if p in track_group})

    cameras = []
    for tr in video_tracks:
        speakers = frozenset(video_map.get(tr["index"], []))
        if speakers:
            cameras.append({"trackIndex": tr["index"], "speakers": speakers})
    if not cameras:
        fail("No video track has any speakers assigned.")

    all_speakers = frozenset(range(n_speakers))
    wide_cam = next((c for c in cameras if c["speakers"] == all_speakers), None)

    cfg = dict(saved)
    cfg.pop("audioMap", None)
    cfg.pop("videoMap", None)
    cfg.pop("speakers", None)
    cfg.pop("sensitivity", None)
    cfg.update({
        "minShot": settings["minShot"],
        "interject": settings["interject"],
        "wideFreq": settings["wideFreq"],
        "wideMin": settings["wideMin"],
    })
    cfg.setdefault("mappings", {})[maps_key] = {
        "videoMap": {str(k): v for k, v in state["videoSel"].items()},
        "names": state["names"],
    }
    save_config(cfg)

    total_hops = int(math.ceil((tl_end - tl_start) / fps / HOP_S))
    if total_hops <= 0:
        fail("The timeline is empty.")

    print("=" * 60)
    print("Podcast Multicam Editor: {} speakers, {} cameras, {:.0f} min"
          .format(n_speakers, len(cameras), (tl_end - tl_start) / fps / 60))

    PROGRESS = ProgressWindow()
    progress_update("Preparing audio analysis…", 0.0)

    levels = build_speaker_levels(
        ffmpeg, audio_tracks, audio_map, n_speakers, fps, tl_start,
        total_hops)
    progress_update("Detecting speech…")
    activity, dominant, stats = compute_activity(
        levels, float(settings["interject"]), speaker_names)
    for line in stats:
        print(line)

    progress_update("Planning the cut…")
    shots = plan_shots(
        activity, dominant, cameras, wide_cam, total_hops,
        float(settings["minShot"]), WIDE_FREQ_PRESETS[settings["wideFreq"]],
        float(settings.get("wideMin", "3.0")))
    print("Planned {} shots.".format(len(shots)))

    if len(shots) <= 1:
        fail("Found no reason to cut anywhere - the result would be one "
             "camera for the whole runtime.\n\nDiagnostics:\n{}\n\nCheck "
             "that every speaker has their own named audio track and that "
             "the microphones are not recorded extremely low."
             .format("\n".join(stats)))

    media_pool = project.GetMediaPool()
    new_tl, n_clips, skipped = build_timeline(
        project, media_pool, timeline, shots, cameras, wide_cam,
        video_tracks, fps, tl_start)

    PROGRESS.close()
    PROGRESS = None

    msg = ("Done! Created timeline \"{}\" with {} shots.\nCameras stayed "
           "on their own tracks; audio is an untouched duplicate of the "
           "original.\n\n{}"
           .format(new_tl.GetName(), n_clips, "\n".join(stats)))
    if skipped:
        msg += "\n({} segments skipped - no media on any camera.)".format(
            skipped)
    show_message("Podcast Multicam Editor", msg)
    maybe_offer_update()


try:
    main()
except SystemExit:
    pass
except Exception:
    import traceback
    _err = traceback.format_exc()
    try:
        print(_err)
    except Exception:
        pass
    try:
        fail("Unexpected error - please report this:\n\n" + _err[-850:] +
             "\n\nVersion {} on {}".format(__version__, sys.platform))
    except Exception:
        pass
