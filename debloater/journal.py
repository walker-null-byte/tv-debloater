"""Persistent, append-only journal of every change, with undo information."""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Journal:
    def __init__(self, path: Path | None = None):
        env = os.environ.get("TVD_JOURNAL")
        self.path = path or (Path(env) if env else (ROOT / "journal" / "journal.jsonl"))
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ io --

    def _all(self) -> list[dict]:
        if not self.path.exists():
            return []
        entries = []
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries

    def ensure_writable(self):
        """Fail early if the journal cannot be written. Called BEFORE any device
        change, so we never modify the TV without a record of it. Re-creates the
        folder if it was deleted while the server was running."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a"):
            pass

    def append(self, entry: dict) -> dict:
        entry = {
            "id": entry.get("id") or uuid.uuid4().hex[:12],
            "ts": entry.get("ts") or _now(),
            **entry,
        }
        # self-heal: the folder may have been deleted while we were running
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    # --------------------------------------------------------------- query --

    def entries(self, serial: str | None = None, limit: int = 500) -> list[dict]:
        rows = [e for e in self._all() if not serial or e.get("serial") == serial]
        return list(reversed(rows))[:limit]

    def batches(self, serial: str | None = None) -> list[dict]:
        grouped: dict[str, dict] = {}
        for e in self._all():
            if serial and e.get("serial") != serial:
                continue
            key = e.get("batch_id") or f"single-{e['id']}"
            batch = grouped.setdefault(key, {
                "batch_id": key, "ts": e["ts"], "serial": e.get("serial"),
                "device": e.get("device"), "action": e.get("action"),
                "note": e.get("note", ""), "items": [], "ok": 0, "failed": 0,
            })
            batch["items"].append(e)
            batch["ts"] = max(batch["ts"], e["ts"])
            if e.get("ok"):
                batch["ok"] += 1
            else:
                batch["failed"] += 1
        return sorted(grouped.values(), key=lambda b: b["ts"], reverse=True)

    def find(self, entry_id: str) -> dict | None:
        return next((e for e in self._all() if e["id"] == entry_id), None)

    # -------------------------------------------------------------- export --

    def undo_script(self, serial: str | None = None) -> str:
        """A shell script that reverses everything still applied."""
        lines = ["#!/bin/sh",
                 "# Auto-generated undo script - reverses all TVDebloater changes.",
                 "# Run after: adb connect <tv-ip>:5555", ""]
        done = set()
        for e in self._all():
            if serial and e.get("serial") != serial:
                continue
            undo = e.get("undo_command")
            key = (e.get("package"), e.get("action"))
            if e.get("ok") and undo and key not in done:
                done.add(key)
                lines.append(f"adb shell {undo}    # undo {e.get('action')} of {e.get('package')}")
        if len(lines) == 4:
            lines.append("# (nothing to undo yet)")
        return "\n".join(lines) + "\n"

    def markdown_report(self, serial: str | None = None, device: dict | None = None) -> str:
        entries = [e for e in self._all() if not serial or e.get("serial") == serial]
        device = device or {}
        date = time.strftime("%Y-%m-%d %H:%M")
        out = [f"# TV Debloat report - {date}", ""]
        if device:
            out += ["## Device", "",
                    f"- Model: **{device.get('model','?')}** ({device.get('brand','?')}, "
                    f"Android {device.get('android_version','?')})",
                    f"- Serial: `{device.get('serial','?')}`", ""]
        applied = [e for e in entries if e.get("ok")]
        failed = [e for e in entries if not e.get("ok")]
        out += [f"## Summary", "",
                f"- Changes applied: **{len(applied)}** ({len(failed)} failed)",
                ""]
        out += ["## Applied changes", ""]
        if applied:
            out.append("| when | action | package | what it was | undo |")
            out.append("|---|---|---|---|---|")
            for e in applied:
                desc = (e.get("label") or "").replace("|", "/")
                undo = (e.get("undo_command") or "-").replace("|", "/")
                out.append(f"| {e['ts']} | {e.get('action')} | `{e.get('package')}` | {desc} | `{undo}` |")
        else:
            out.append("_none_")
        if failed:
            out += ["", "## Failed attempts", ""]
            for e in failed:
                out.append(f"- `{e.get('package')}` {e.get('action')}: {e.get('output','')[:120]}")
        out += ["", "## One-shot undo", "",
                "Run `reports/undo.sh` (also downloadable from the Tune & Guide tab) or:", "",
                "```sh"]
        for e in applied:
            if e.get("undo_command"):
                out.append(f"adb shell {e['undo_command']}")
        out.append("```")
        return "\n".join(out) + "\n"
