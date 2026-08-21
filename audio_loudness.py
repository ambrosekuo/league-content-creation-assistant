#!/usr/bin/env python3
"""Cheap per-clip loudness features (ffmpeg volumedetect, audio-only).

Relative spike vs the game's median speaking level is the useful signal.
Does not decode video. Typical cost: well under a second per ~20s clip.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MEAN_RE = re.compile(r"mean_volume:\s*([-\d.]+)\s*dB")
MAX_RE = re.compile(r"max_volume:\s*([-\d.]+)\s*dB")


def probe_volume(path: Path) -> dict[str, float]:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(path),
            "-vn",
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        text=True,
        capture_output=True,
    )
    text = (proc.stderr or "") + (proc.stdout or "")
    mean_m = MEAN_RE.search(text)
    max_m = MAX_RE.search(text)
    mean_db = float(mean_m.group(1)) if mean_m else 0.0
    max_db = float(max_m.group(1)) if max_m else mean_db
    return {
        "meanDb": round(mean_db, 3),
        "maxDb": round(max_db, 3),
        "rangeDb": round(max_db - mean_db, 3),
    }


def reaction_bonus(spike_db: float) -> float:
    if spike_db >= 10.0:
        return 2.0
    if spike_db >= 6.0:
        return 1.0
    if spike_db >= 4.0:
        return 0.4
    return 0.0


def analyze_clips_dir(clips_dir: Path) -> dict[str, Any]:
    clips: list[dict[str, Any]] = []
    for path in sorted(clips_dir.rglob("c*.mp4")):
        if not path.is_file() or "lobby" in path.name.lower():
            continue
        folder = path.parent.name
        vol = probe_volume(path)
        row = {
            "relativePath": path.relative_to(clips_dir).as_posix(),
            "gameFolder": folder,
            **vol,
        }
        clips.append(row)
        print(
            f"[audio] {row['relativePath']}  mean={vol['meanDb']:.1f}  "
            f"max={vol['maxDb']:.1f}  range={vol['rangeDb']:.1f}",
            flush=True,
        )

    by_game: dict[str, list[dict[str, Any]]] = {}
    for clip in clips:
        by_game.setdefault(clip["gameFolder"], []).append(clip)
    for group in by_game.values():
        means = [float(c["meanDb"]) for c in group]
        baseline = statistics.median(means) if means else 0.0
        for clip in group:
            spike = float(clip["maxDb"]) - baseline
            clip["baselineDb"] = round(baseline, 3)
            clip["spikeDb"] = round(spike, 3)
            clip["reactionBonus"] = reaction_bonus(spike)

    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "clips_dir": str(clips_dir),
        "clip_count": len(clips),
        "note": "spikeDb = clip maxDb minus game median meanDb (relative, not absolute)",
        "clips": clips,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Extract relative loudness per lol_clips mp4")
    p.add_argument("--clips-dir", type=Path, required=True)
    p.add_argument("--output", type=Path, default=None)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    clips_dir = args.clips_dir.resolve()
    if not clips_dir.is_dir():
        print(f"missing {clips_dir}", flush=True)
        return 1
    payload = analyze_clips_dir(clips_dir)
    out = (args.output or clips_dir / "audio_features.json").resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": "ok", "output": str(out), "clip_count": payload["clip_count"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
