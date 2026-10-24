"""Append-only JSONL journal plus an atomically written state file, so a run can
be inspected while it is going and resumed after a restart."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .clock import fmt_ts


class Journal:
    def __init__(self, run_dir: str | Path):
        self.dir = Path(run_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "journal.jsonl"
        self.state_path = self.dir / "state.json"

    def log(self, kind: str, ts: float, **data) -> dict:
        event = {"ts": ts, "time": fmt_ts(ts), "kind": kind, **data}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, default=str, ensure_ascii=False) + "\n")
        return event

    def events(self, kinds: set[str] | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                if kinds is None or event.get("kind") in kinds:
                    out.append(event)
        return out

    def save_state(self, state: dict) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def load_state(self) -> dict | None:
        if not self.state_path.exists():
            return None
        return json.loads(self.state_path.read_text(encoding="utf-8"))
