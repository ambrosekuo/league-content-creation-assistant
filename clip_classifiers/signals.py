"""Extract compact deterministic signals for classifiers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from score_clips import norm_type, same_champ, voice_from_text, voice_score

from clip_classifiers.types import ClipSignals

EVENT_TYPES = frozenset({"KILL", "DEATH", "ASSIST"})
OBJECTIVE_TYPES = frozenset(
    {"BARON", "DRAGON", "ELDER", "HERALD", "HORDE", "INHIBITOR", "TOWER", "NEXUS", "GAME_END"}
)


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def manifest_entry(dataset_dir: Path | None, relative_path: str) -> dict[str, Any] | None:
    if dataset_dir is None:
        return None
    manifest = _load_json(dataset_dir / "lol_clips" / "clips.json")
    if not manifest:
        versioned = sorted(dataset_dir.glob("lol_clips_*/clips.json"))
        if versioned:
            manifest = _load_json(versioned[-1])
    if not manifest:
        return None
    needle = relative_path.lstrip("/")
    if needle.startswith("lol_clips/"):
        needle = needle.split("/", 1)[1]
    for item in manifest.get("clips") or []:
        rel = str(item.get("relativePath") or item.get("filename") or "").lstrip("/")
        if rel == needle or rel.endswith(f"/{needle}") or needle.endswith(rel):
            return item
    return None


def clip_window(clip: dict[str, Any], manifest: dict[str, Any] | None) -> tuple[float | None, float | None]:
    start = None
    end = None
    if manifest:
        start = manifest.get("localStart") or manifest.get("start")
        end = manifest.get("localEnd") or manifest.get("end")
    if start is None:
        start = clip.get("vodTimeSeconds")
    if end is None and clip.get("duration") is not None and start is not None:
        try:
            end = float(start) + float(clip["duration"])
        except (TypeError, ValueError):
            end = None
    try:
        start_f = float(start) if start is not None else None
    except (TypeError, ValueError):
        start_f = None
    try:
        end_f = float(end) if end is not None else None
    except (TypeError, ValueError):
        end_f = None
    return start_f, end_f


def _collect_timed_events(dataset_dir: Path | None) -> list[dict[str, Any]]:
    if dataset_dir is None:
        return []
    payload = _load_json(dataset_dir / "lol_events.json")
    if not payload:
        payload = _load_json(dataset_dir / "lol_events_snapped.json")
    if not payload:
        return []
    rows: list[dict[str, Any]] = []
    for match in payload.get("matches") or []:
        for event in match.get("events") or []:
            offset = event.get("vodOffsetSeconds")
            if offset is None:
                continue
            try:
                t = float(offset)
            except (TypeError, ValueError):
                continue
            rows.append(
                {
                    "type": norm_type(str(event.get("type") or "")),
                    "t": t,
                }
            )
    return rows


def _count_in_window(events: list[dict[str, Any]], center: float, seconds: float, event_type: str) -> int:
    lo = center - seconds
    hi = center + seconds
    return sum(1 for ev in events if ev["type"] == event_type and lo <= ev["t"] <= hi)


def _reaction_level(score: float) -> str:
    if score >= 0.5:
        return "high"
    if score >= 0.25:
        return "medium"
    return "low"


def extract_signals(clip: dict[str, Any], *, dataset_dir: Path | None) -> ClipSignals:
    types = [norm_type(t) for t in (clip.get("types") or [])]
    types = [t for t in types if t]
    died = "DEATH" in types
    manifest = manifest_entry(dataset_dir, str(clip.get("relativePath") or ""))
    start, end = clip_window(clip, manifest)

    transcript = _load_json(dataset_dir / "transcript.json") if dataset_dir else None
    segments = list((transcript or {}).get("segments") or [])
    voice_pts, hits, _text = voice_score(segments, start, end, died=died)
    if not _text and manifest:
        snippet = manifest.get("transcript")
        if isinstance(snippet, str) and snippet.strip():
            voice_pts, hits = voice_from_text(snippet.strip(), died=died)

    center = None
    if start is not None and end is not None:
        center = (start + end) / 2.0
    elif start is not None:
        center = start

    timed = _collect_timed_events(dataset_dir)
    if center is not None and timed:
        kills_in_10s = _count_in_window(timed, center, 10.0, "KILL")
        deaths_in_10s = _count_in_window(timed, center, 10.0, "DEATH")
        assists_in_10s = _count_in_window(timed, center, 10.0, "ASSIST")
    else:
        kills_in_10s = types.count("KILL")
        deaths_in_10s = types.count("DEATH")
        assists_in_10s = types.count("ASSIST")

    opponent = clip.get("opponentChampion")
    lane = clip.get("laneOpponentChampion")
    vs_lane = same_champ(str(opponent or ""), str(lane or ""))

    duration = clip.get("duration")
    try:
        duration_f = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_f = None

    # v1 stub — vision/HP detection comes later.
    low_hp = False

    game_end_nearby = "GAME_END" in types or "NEXUS" in types
    if not game_end_nearby and center is not None and timed:
        game_end_nearby = any(ev["type"] in {"GAME_END", "NEXUS"} and abs(ev["t"] - center) <= 20 for ev in timed)

    return ClipSignals(
        event_type=[t.lower() for t in types],
        kills_in_10s=kills_in_10s,
        deaths_in_10s=deaths_in_10s,
        assists_in_10s=assists_in_10s,
        low_hp=low_hp,
        game_end_nearby=game_end_nearby,
        reaction_score=round(min(1.0, max(0.0, voice_pts)), 3),
        reaction_level=_reaction_level(voice_pts),
        champion=str(clip.get("champion") or "Unknown"),
        opponent=str(opponent) if opponent else None,
        lane_opponent=str(lane) if lane else None,
        vs_lane=vs_lane,
        died=died,
        win=clip.get("win"),
        duration=duration_f,
        voice_hits=list(hits),
    )
