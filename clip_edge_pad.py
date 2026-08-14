"""Shared edge pad (cut) / freeze trim (stitch) for seek-cut open/close freezes.

Cut expands each window by PAD_LEAD / PAD_TRAIL so there is footage to
trim into. Stream-copy cuts start audio at t=0 but video only at the next
keyframe (often 1–2s later) — players hold the first frame until then.
Stitch drops that packet gap, plus any frozen frames at the start/end,
instead of always cutting a fixed 1.5s.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

# Keep cut --pad-* defaults; stitch uses freeze detection by default.
PAD_LEAD_S = 1.5
PAD_TRAIL_S = 1.5

_FREEZE_START = re.compile(r"freeze_start:\s*([0-9.]+)")
_FREEZE_END = re.compile(r"freeze_end:\s*([0-9.]+)")


def _video_timeline(path: Path) -> tuple[float, float, float]:
    """Return (format_duration, video_start, video_end) in seconds."""
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=start_time,duration",
            "-show_entries",
            "format=duration,start_time",
            "-of",
            "json",
            str(path),
        ],
        text=True,
        capture_output=True,
    )
    data = json.loads(proc.stdout or "{}") if proc.returncode == 0 else {}
    fmt = data.get("format") or {}
    stream = (data.get("streams") or [{}])[0]
    try:
        fmt_dur = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        fmt_dur = 0.0
    try:
        fmt_start = float(fmt.get("start_time") or 0.0)
    except (TypeError, ValueError):
        fmt_start = 0.0
    try:
        v_start = float(stream.get("start_time") or fmt_start or 0.0)
    except (TypeError, ValueError):
        v_start = fmt_start
    try:
        v_dur = float(stream.get("duration") or 0.0)
    except (TypeError, ValueError):
        v_dur = 0.0
    v_end = (v_start + v_dur) if v_dur > 0 else fmt_dur
    return fmt_dur, max(0.0, v_start), max(v_start, v_end)


def _freezedetect_pairs(
    path: Path,
    *,
    window_s: float,
    noise: float,
    min_duration: float,
    from_end: bool,
) -> list[tuple[float, float]]:
    """Return [(start, end), ...] in the scanned window. Unclosed start → EOF."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info"]
    if from_end:
        cmd += ["-sseof", f"-{window_s:.3f}"]
    cmd += [
        "-i",
        str(path),
        "-t",
        f"{window_s:.3f}",
        "-an",
        "-vf",
        f"freezedetect=n={noise:.4f}:d={min_duration:.3f}",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(cmd, text=True, capture_output=True)
    text = (proc.stderr or "") + (proc.stdout or "")
    starts = [float(m.group(1)) for m in _FREEZE_START.finditer(text)]
    ends = [float(m.group(1)) for m in _FREEZE_END.finditer(text)]
    pairs: list[tuple[float, float]] = []
    for i, start in enumerate(starts):
        if i < len(ends):
            pairs.append((start, ends[i]))
        else:
            pairs.append((start, window_s))
    return pairs


def detect_edge_freezes(
    path: Path,
    *,
    window_s: float = 4.0,
    noise: float = 0.004,
    min_duration: float = 0.12,
    extra_s: float = 0.04,
    max_trim_s: float = 4.0,
    keep_at_least_s: float = 0.75,
    duration: float | None = None,
) -> tuple[float, float]:
    """Seconds of frozen / not-yet-decoded video to drop from (start, end)."""
    window_s = max(0.4, float(window_s))
    extra_s = max(0.0, float(extra_s))
    max_trim_s = max(0.0, float(max_trim_s))
    lead = 0.0
    trail = 0.0
    dur = max(0.0, float(duration or 0.0))
    try:
        fmt_dur, v_start, v_end = _video_timeline(path)
    except Exception:
        fmt_dur, v_start, v_end = dur, 0.0, dur
    if dur <= 0:
        dur = fmt_dur
    # Stream-copy seek: audio at 0, first video packet at the next keyframe.
    if v_start > 0.04:
        lead = v_start
    if fmt_dur > 0 and v_end > 0 and (fmt_dur - v_end) > 0.04:
        trail = fmt_dur - v_end
    end_window = window_s if dur <= 0 else min(window_s, dur)
    origin = v_start if v_start > 0.04 else 0.0
    try:
        for start, end in _freezedetect_pairs(
            path,
            window_s=window_s,
            noise=noise,
            min_duration=min_duration,
            from_end=False,
        ):
            if start <= origin + 0.08:
                lead = max(lead, end)
        for start, end in _freezedetect_pairs(
            path,
            window_s=end_window,
            noise=noise,
            min_duration=min_duration,
            from_end=True,
        ):
            if end >= end_window - 0.12:
                trail = max(trail, end_window - start)
    except Exception:
        return round(lead, 3), round(trail, 3)

    if lead > 0:
        lead = min(max_trim_s, lead + extra_s)
    if trail > 0:
        trail = min(max_trim_s, trail + extra_s)

    dur = float(duration or 0.0)
    if dur <= 0:
        return round(lead, 3), round(trail, 3)
    keep = max(0.2, float(keep_at_least_s))
    if lead + trail > dur - keep:
        # Prefer cutting the longer freeze; never eat the whole clip.
        budget = max(0.0, dur - keep)
        if lead >= trail:
            lead = min(lead, budget)
            trail = min(trail, max(0.0, budget - lead))
        else:
            trail = min(trail, budget)
            lead = min(lead, max(0.0, budget - trail))
    return round(lead, 3), round(trail, 3)
