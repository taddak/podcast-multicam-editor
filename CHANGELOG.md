# Changelog

## 1.0.0 (2026-07-19)

First public release.

- Automatic multicam cutting driven by per-microphone speech detection
  (Otsu thresholds, crosstalk suppression, dominant-speaker resolution).
- Checkerboard editing: every shot stays on its camera's own video track;
  audio tracks and their post-processing are preserved via timeline
  duplication. The original timeline is never modified.
- Reaction cuts on short overlaps, wide shot on sustained overlaps and
  monologues, early cuts into pauses.
- Timeline-style configuration UI with per-track speaker naming, colors and
  per-project memory.
- Parallel audio analysis with a progress window.
- Runtime calibration of Resolve's AppendToTimeline behaviour.
- Self-update check against this repository.
- macOS, Windows and Linux support.
