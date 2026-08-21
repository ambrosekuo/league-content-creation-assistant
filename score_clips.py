#!/usr/bin/env python3
"""Rank already-cut LoL clips and pick the top K per game.

Used by stitch_game_clips / cloud_job process-clips. Highlight score rewards
kills you survive; deaths and GAME_END do not compete for that rank. Voice is
a small sentiment bonus. Daily picks add diversity caps + at most one closer.

Writes:
  lol_clips/clip_scores.json   all clips, ranked
  lol_clips/top_picks.json     top K (chrono within each game / day)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from score_windows import exclamation_bonus
from dataset_paths import find_dataset_dir

FOLDER_RE = re.compile(
    r"^g(?P<game>\d+)_(?P<champ>[A-Za-z0-9]+)(?:_vs(?P<lane>[A-Za-z0-9]+))?$",
    re.IGNORECASE,
)
STEM_RE = re.compile(
    r"^c(?P<idx>\d+)_(?P<label>.+?)(?:_vs(?P<opp>[A-Za-z0-9]+))?$",
    re.IGNORECASE,
)

# Additive highlight components. GAME_END is a pick slot, not a type bonus.
TYPE_WEIGHTS: dict[str, float] = {
    "KILL": 3.0,
    "DEATH": -2.5,
    "ASSIST": 0.35,
    "GAME_END": 0.0,
    "BARON": 3.5,
    "DRAGON": 2.0,
    "ELDER": 3.0,
    "HERALD": 1.6,
    "HORDE": 1.2,
    "INHIBITOR": 1.8,
    "TOWER": 0.9,
    "GAME_START": 0.0,
}

LANE_KILL = 2.2
LANE_TRADE = 0.8  # vs-lane kill that also died
LANE_DEATH = 0.0
LANE_OTHER = 0.4
EXTRA_KILL = 1.4  # second+ KILL label in the window
ASSIST_ONLY = -1.8
ASSIST_ONLY_LANE = -0.6
FIRST_KILL = 1.6
FIRST_LANE_SOLO = 2.2  # extra when the opener is a survived 1v1 vs lane
IDEAL_DUR = (8.0, 16.0)
SOFT_DUR = (6.0, 22.0)
VOICE_CAP = 1.0

REACTION_WEIGHTS: dict[str, float] = {
    "oh my god": 1.0,
    "no way": 0.9,
    "insane": 0.8,
    "clip that": 1.0,
    "clip this": 1.0,
    "outplayed": 0.8,
    "let's go": 0.7,
    "lets go": 0.7,
    "pentakill": 1.0,
    "penta": 0.9,
    "quadra": 0.9,
    "triple": 0.7,
    "one shot": 0.6,
    "oneshot": 0.6,
    "holy": 0.5,
    "oh my": 0.4,
}

NEGATIVE_PHRASES: tuple[str, ...] = (
    "pretty bad",
    "that's pretty bad",
    "disaster",
    "tilted",
    "death recap",
    "i actually died",
    "i died",
    "grief",
    "not even good",
    "inted",
    "i inted",
)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def probe_duration(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        text=True,
        capture_output=True,
    )
    try:
        return max(0.0, float((proc.stdout or "0").strip().split(",")[0]))
    except ValueError:
        return 0.0


def norm_type(raw: str) -> str:
    text = str(raw or "").strip().upper().replace("-", "_")
    aliases = {
        "GAMEEND": "GAME_END",
        "GAME_END": "GAME_END",
        "GAMESTART": "GAME_START",
        "GAME_START": "GAME_START",
        "NEXUS": "GAME_END",
    }
    return aliases.get(text) or aliases.get(text.replace("_", "")) or text


def parse_folder(name: str) -> dict[str, Any]:
    m = FOLDER_RE.match(name)
    if not m:
        return {"gameIndex": None, "champion": None, "laneOpponentChampion": None}
    return {
        "gameIndex": int(m.group("game")),
        "champion": m.group("champ"),
        "laneOpponentChampion": m.group("lane"),
    }


def parse_stem(stem: str) -> dict[str, Any]:
    m = STEM_RE.match(stem)
    if not m:
        return {"clipIndexInGame": None, "types": [], "opponentChampion": None}
    label = str(m.group("label") or "")
    types = [norm_type(part) for part in label.split("+") if part.strip()]
    types = [t for t in types if t]
    return {
        "clipIndexInGame": int(m.group("idx")),
        "types": types,
        "opponentChampion": m.group("opp"),
    }


def same_champ(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return "".join(ch for ch in a if ch.isalnum()).lower() == "".join(
        ch for ch in b if ch.isalnum()
    ).lower()


def duration_score(seconds: float, *, has_end: bool = False) -> float:
    if seconds <= 0:
        return 0.0
    lo, hi = IDEAL_DUR
    if lo <= seconds <= hi:
        return 0.8
    slo, shi = SOFT_DUR
    if seconds < slo:
        return max(-1.2, (seconds - slo) * 0.25)
    if seconds <= shi:
        return 0.15
    extra = seconds - shi
    return max(-3.5, -extra * 0.08)


def overlapping_text(segments: list[dict[str, Any]], start: float, end: float) -> list[dict[str, Any]]:
    return [
        s
        for s in segments
        if not (float(s["end"]) < start or float(s["start"]) > end)
    ]


def reaction_score(text: str) -> tuple[float, list[str]]:
    lowered = text.lower()
    score, hits = 0.0, []
    for phrase, weight in REACTION_WEIGHTS.items():
        if phrase in lowered:
            score += weight
            hits.append(phrase)
    return score, hits


def negative_hits(text: str) -> list[str]:
    lowered = text.lower()
    return [p for p in NEGATIVE_PHRASES if p in lowered]


def voice_from_text(text: str, *, died: bool) -> tuple[float, list[str]]:
    text = (text or "").strip()
    if not text:
        return 0.0, []
    kw, kw_hits = reaction_score(text)
    neg = negative_hits(text)
    if neg:
        return round(-min(1.5, 0.8 * len(neg)), 3), [f"-{p}" for p in neg]
    if died:
        return 0.0, []
    excl = min(0.25, exclamation_bonus(text) * 0.15)
    return round(min(VOICE_CAP, kw + excl), 3), kw_hits


def voice_score(
    segments: list[dict[str, Any]] | None,
    start: float | None,
    end: float | None,
    *,
    died: bool = False,
) -> tuple[float, list[str], str]:
    if not segments or start is None or end is None or end <= start:
        return 0.0, [], ""
    hits = overlapping_text(segments, float(start), float(end))
    text = " ".join(str(s.get("text") or "").strip() for s in hits).strip()
    if not text:
        return 0.0, [], ""
    total, tags = voice_from_text(text, died=died)
    return total, tags, text


def score_clip(
    clip: dict[str, Any],
    *,
    first_kill: bool,
    first_lane_solo: bool = False,
) -> dict[str, Any]:
    types = [norm_type(t) for t in (clip.get("types") or [])]
    types = [t for t in types if t]
    has_end = "GAME_END" in types
    died = "DEATH" in types
    vs_lane = bool(clip.get("vsLane"))
    n_kill = types.count("KILL")
    highlight_types = [t for t in types if t != "GAME_END"]
    type_pts = sum(TYPE_WEIGHTS.get(t, 0.4) for t in highlight_types)
    if n_kill > 1:
        type_pts += EXTRA_KILL * (n_kill - 1)

    lane = 0.0
    if vs_lane:
        if "KILL" in types and died:
            lane = LANE_TRADE
        elif "KILL" in types:
            lane = LANE_KILL
        elif died:
            lane = LANE_DEATH
        else:
            lane = LANE_OTHER

    assist_pen = 0.0
    if types and set(highlight_types) <= {"ASSIST"}:
        assist_pen = ASSIST_ONLY_LANE if vs_lane else ASSIST_ONLY

    opener = 0.0
    if first_kill and "KILL" in types:
        opener += FIRST_KILL
        # First blood still counts if you traded; don't let DEATH bury it.
        if died:
            type_pts -= TYPE_WEIGHTS.get("DEATH", 0.0)
    if first_lane_solo:
        opener += FIRST_LANE_SOLO
    dur = duration_score(float(clip.get("duration") or 0.0), has_end=has_end)
    voice = float((clip.get("voice") or {}).get("score") or 0.0)

    total = type_pts + lane + assist_pen + opener + dur + voice

    closer = 0.0
    if has_end:
        closer = 2.0
        if "KILL" in types:
            closer += 2.2
        if clip.get("win") is True:
            closer += 1.5
        elif clip.get("win") is False:
            closer -= 0.4
        if vs_lane and "KILL" in types:
            closer += 0.8
        if died:
            closer -= 1.5
        closer += dur

    why: list[str] = []
    if vs_lane and "KILL" in types and not died:
        why.append("lane kill")
    elif vs_lane and "KILL" in types:
        why.append("lane trade")
    elif vs_lane:
        why.append("vs lane")
    if died and "KILL" in types:
        why.append("died")
    elif died:
        why.append("death")
    elif "KILL" in types:
        why.append("survived")
    if has_end:
        why.append("game end")
    if assist_pen < -1:
        why.append("assist only")
    if first_lane_solo:
        why.append("first lane solo")
    elif first_kill:
        why.append("first kill")
    if dur < -1:
        why.append("long")
    if voice < 0:
        why.append("neg voice")
    elif voice > 0:
        why.append("reaction")

    return {
        **clip,
        "score": round(total, 3),
        "closerScore": round(closer, 3),
        "components": {
            "type": round(type_pts, 3),
            "lane": round(lane, 3),
            "combo": 0.0,
            "assist": round(assist_pen, 3),
            "opener": round(opener, 3),
            "duration": round(dur, 3),
            "voice": round(voice, 3),
            "closer": round(closer, 3),
        },
        "why": why,
    }


def game_key(clip: dict[str, Any]) -> str:
    return f"{clip.get('vodId') or ''}:{clip.get('gameFolder') or clip.get('gameIndex') or ''}"


def infer_vod_id(path: Path, root: Path) -> str | None:
    try:
        parts = path.resolve().relative_to(root.resolve()).parts
    except ValueError:
        parts = path.parts
    for part in parts:
        if part.isdigit() and len(part) >= 8:
            return part
    return None


def _clip_row_from_path(
    path: Path,
    clips_dir: Path,
    *,
    item: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = item or {}
    folder_meta = parse_folder(path.parent.name)
    stem = parse_stem(path.stem)
    types = [norm_type(t) for t in (item.get("types") or stem["types"] or [])]
    rel = path.relative_to(clips_dir).as_posix()
    return {
        "relativePath": rel,
        "path": str(path),
        "filename": path.name,
        "gameFolder": path.parent.name,
        "vodId": infer_vod_id(path, clips_dir),
        "gameIndex": item.get("gameIndex") or folder_meta.get("gameIndex"),
        "clipIndexInGame": item.get("clipIndexInGame") or stem.get("clipIndexInGame"),
        "champion": item.get("champion") or folder_meta.get("champion"),
        "laneOpponentChampion": item.get("laneOpponentChampion")
        or folder_meta.get("laneOpponentChampion"),
        "opponentChampion": item.get("opponentChampion") or stem.get("opponentChampion"),
        "types": types,
        "win": item.get("win"),
        "matchId": item.get("matchId"),
        "start": item.get("start") or item.get("windowLocalStart") or item.get("localStart"),
        "end": item.get("end") or item.get("windowLocalEnd") or item.get("localEnd"),
        "duration": float(item.get("duration") or 0.0),
        "transcript": item.get("transcript"),
    }


def discover_clips(clips_dir: Path) -> list[dict[str, Any]]:
    """Find c*.mp4 under gNN_* folders, including nested day/VOD layouts."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for manifest_path in sorted(clips_dir.rglob("clips.json")):
        try:
            payload = load_json(manifest_path)
        except (OSError, json.JSONDecodeError):
            continue
        base = manifest_path.parent
        for item in payload.get("clips") or []:
            rel = str(item.get("relativePath") or item.get("filename") or "")
            if not rel:
                continue
            path = (base / rel)
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            rows.append(_clip_row_from_path(path, clips_dir, item=item))
    if rows:
        return rows

    for path in sorted(clips_dir.rglob("c*.mp4")):
        if not path.is_file() or not FOLDER_RE.match(path.parent.name):
            continue
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        rows.append(_clip_row_from_path(path, clips_dir))
    return rows


def enrich(clips: list[dict[str, Any]], *, transcript: dict[str, Any] | None) -> None:
    segments = list((transcript or {}).get("segments") or [])
    for clip in clips:
        dur = float(clip.get("duration") or 0.0)
        path = Path(clip["path"])
        if dur <= 0.1 and path.is_file():
            clip["duration"] = round(probe_duration(path), 3)
        clip["vsLane"] = same_champ(
            str(clip.get("opponentChampion") or ""),
            str(clip.get("laneOpponentChampion") or ""),
        )
        v_score, hits, text = voice_score(
            segments,
            clip.get("start"),
            clip.get("end"),
            died="DEATH" in (clip.get("types") or []),
        )
        snippet = clip.get("transcript")
        if isinstance(snippet, str) and snippet.strip() and not text:
            text = snippet.strip()
            v_score, hits = voice_from_text(
                text, died="DEATH" in (clip.get("types") or [])
            )
        clip["voice"] = {"score": v_score, "keyword_hits": hits, "text": text}

    by_game: dict[str, list[dict[str, Any]]] = {}
    for clip in clips:
        by_game.setdefault(game_key(clip), []).append(clip)
    for group in by_game.values():
        ordered = sorted(group, key=lambda c: int(c.get("clipIndexInGame") or 10**6))
        marked_kill = False
        marked_solo = False
        for clip in ordered:
            types = [norm_type(t) for t in (clip.get("types") or [])]
            types = [t for t in types if t]
            died = "DEATH" in types
            highlight = [t for t in types if t != "GAME_END"]
            if not marked_kill and "KILL" in types:
                clip["firstKill"] = True
                marked_kill = True
            else:
                clip["firstKill"] = False
            lane_solo = (
                bool(clip.get("vsLane"))
                and "KILL" in types
                and not died
                and set(highlight) <= {"KILL"}
            )
            if not marked_solo and lane_solo:
                clip["firstLaneSolo"] = True
                marked_solo = True
            else:
                clip["firstLaneSolo"] = False


def rank_dataset(dataset_dir: Path, clips_dir: Path) -> list[dict[str, Any]]:
    """Score every clip under clips_dir. Returns score-sorted rows with rank."""
    clips = discover_clips(clips_dir)
    if not clips:
        return []
    transcript = None
    tpath = dataset_dir / "transcript.json"
    if tpath.is_file():
        transcript = load_json(tpath)
    enrich(clips, transcript=transcript)
    scored = [
        score_clip(
            c,
            first_kill=bool(c.get("firstKill")),
            first_lane_solo=bool(c.get("firstLaneSolo")),
        )
        for c in clips
    ]
    scored.sort(key=lambda c: float(c["score"]), reverse=True)
    for i, clip in enumerate(scored, start=1):
        clip["rank"] = i
    return scored


def matchup_key(clip: dict[str, Any]) -> str:
    champ = str(clip.get("champion") or "").lower()
    lane = str(
        clip.get("laneOpponentChampion") or clip.get("opponentChampion") or ""
    ).lower()
    return f"{champ}|{lane}"


def pick_top_k(
    ranked: list[dict[str, Any]],
    k: int,
    *,
    per_game: bool = True,
    max_per_game: int = 0,
    order: str = "chrono",
    max_closers: int = 1,
    max_death_per_game: int = 1,
    max_per_matchup: int = 2,
    closer_needs_kill: bool | None = None,
) -> list[dict[str, Any]]:
    """Keep the best K clips.

    per_game=True: K clips from each game (match recap).
    per_game=False: K clips from the whole pool (daily). max_per_game caps
    how many one game can contribute. GAME_END clips are a separate slot
    (at most max_closers) so they do not beat lane solos on stacked type points.
    """
    if closer_needs_kill is None:
        closer_needs_kill = not per_game
    if k <= 0:
        picked = list(ranked)
    elif per_game:
        picked = []
        by_game: dict[str, list[dict[str, Any]]] = {}
        for clip in ranked:
            by_game.setdefault(game_key(clip), []).append(clip)
        per_game_deaths = max(max_death_per_game, 2)
        for group in by_game.values():
            picked.extend(
                _select_slate(
                    group,
                    k,
                    max_per_game=k,
                    max_closers=max_closers,
                    max_death_per_game=per_game_deaths,
                    max_per_matchup=0,
                    closer_needs_kill=bool(closer_needs_kill),
                )
            )
    else:
        picked = _select_slate(
            ranked,
            k,
            max_per_game=max_per_game,
            max_closers=max_closers,
            max_death_per_game=max_death_per_game,
            max_per_matchup=max_per_matchup,
            closer_needs_kill=bool(closer_needs_kill),
        )
    return _order_picks(picked, order=order)


def _opener_clips(group: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """First blood (even a trade) and first survived lane solo, chrono, unique."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    ordered = sorted(group, key=lambda c: int(c.get("clipIndexInGame") or 10**6))
    for clip in ordered:
        if not (clip.get("firstKill") or clip.get("firstLaneSolo")):
            continue
        rel = str(clip.get("relativePath") or "")
        if not rel or rel in seen:
            continue
        seen.add(rel)
        out.append(clip)
    return out


def _chrono_key(clip: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(clip.get("vodId") or ""),
        int(clip.get("gameIndex") or 0),
        int(clip.get("clipIndexInGame") or 0),
    )


def _order_picks(picked: list[dict[str, Any]], *, order: str) -> list[dict[str, Any]]:
    out = list(picked)
    if str(order).strip().lower() == "score":
        out.sort(key=lambda c: float(c.get("score") or 0.0), reverse=True)
    else:
        out.sort(key=_chrono_key)
    return out


def _iter_game_groups(ranked: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    by_game: dict[str, list[dict[str, Any]]] = {}
    for clip in ranked:
        by_game.setdefault(game_key(clip), []).append(clip)
    return list(by_game.values())


def _select_top_score(
    group: list[dict[str, Any]],
    k: int,
    *,
    max_death_per_game: int = 2,
) -> list[dict[str, Any]]:
    """Top K by content score. GAME_END competes; no reserved closer slot."""
    ranked = sorted(group, key=lambda c: float(c.get("score") or 0.0), reverse=True)
    picked: list[dict[str, Any]] = []
    deaths = 0
    seen: set[str] = set()

    def take(clip: dict[str, Any], *, ignore_death_cap: bool = False) -> bool:
        nonlocal deaths
        if k > 0 and len(picked) >= k:
            return False
        rel = str(clip.get("relativePath") or "")
        if not rel or rel in seen:
            return False
        types = clip.get("types") or []
        if (
            not ignore_death_cap
            and "DEATH" in types
            and max_death_per_game > 0
            and deaths >= max_death_per_game
        ):
            return False
        picked.append(clip)
        seen.add(rel)
        if "DEATH" in types:
            deaths += 1
        return True

    for clip in _opener_clips(group):
        take(clip, ignore_death_cap=True)
    for clip in ranked:
        take(clip)
    return picked


def pick_top_score(
    ranked: list[dict[str, Any]],
    k: int,
    *,
    per_game: bool = True,
    order: str = "chrono",
    max_death_per_game: int = 2,
) -> list[dict[str, Any]]:
    if k <= 0:
        picked = list(ranked)
    elif per_game:
        picked = []
        for group in _iter_game_groups(ranked):
            picked.extend(
                _select_top_score(group, k, max_death_per_game=max_death_per_game)
            )
    else:
        picked = _select_top_score(ranked, k, max_death_per_game=max_death_per_game)
    return _order_picks(picked, order=order)


def _select_duration_budget(
    group: list[dict[str, Any]],
    *,
    min_score: float,
    max_duration: float,
    min_clips: int = 2,
    max_same_opp: int = 3,
) -> list[dict[str, Any]]:
    """Keep clips above a score floor until a duration budget is full."""
    ranked = sorted(group, key=lambda c: float(c.get("score") or 0.0), reverse=True)
    picked: list[dict[str, Any]] = []
    used = 0.0
    seen: set[str] = set()
    opp_n: dict[str, int] = {}

    def try_take(clip: dict[str, Any], *, ignore_floor: bool) -> bool:
        nonlocal used
        rel = str(clip.get("relativePath") or "")
        if not rel or rel in seen:
            return False
        score = float(clip.get("score") or 0.0)
        if not ignore_floor and score < min_score:
            return False
        dur = max(0.0, float(clip.get("duration") or 0.0))
        if picked and max_duration > 0 and used + dur > max_duration:
            return False
        opp = str(
            clip.get("opponentChampion") or clip.get("laneOpponentChampion") or ""
        ).lower()
        if opp and opp_n.get(opp, 0) >= max_same_opp and score < 5.5:
            if not clip.get("firstKill"):
                return False
        picked.append(clip)
        seen.add(rel)
        used += dur
        if opp:
            opp_n[opp] = opp_n.get(opp, 0) + 1
        return True

    for clip in _opener_clips(group):
        try_take(clip, ignore_floor=True)
    for clip in ranked:
        try_take(clip, ignore_floor=False)
    if len(picked) < min_clips:
        for clip in ranked:
            if len(picked) >= min_clips:
                break
            try_take(clip, ignore_floor=True)
    return picked


def pick_duration_budget(
    ranked: list[dict[str, Any]],
    *,
    per_game: bool = True,
    min_score: float = 3.5,
    max_duration: float = 150.0,
    min_clips: int = 2,
    order: str = "chrono",
) -> list[dict[str, Any]]:
    if per_game:
        picked: list[dict[str, Any]] = []
        for group in _iter_game_groups(ranked):
            picked.extend(
                _select_duration_budget(
                    group,
                    min_score=min_score,
                    max_duration=max_duration,
                    min_clips=min_clips,
                )
            )
    else:
        picked = _select_duration_budget(
            ranked,
            min_score=min_score,
            max_duration=max_duration,
            min_clips=min_clips,
        )
    return _order_picks(picked, order=order)


SELECTORS = ("current", "competitive", "top8", "duration")


def pick_by_selector(
    ranked: list[dict[str, Any]],
    selector: str,
    *,
    per_game: bool = True,
    order: str = "chrono",
    top_k: int | None = None,
    min_score: float = 3.5,
    max_duration: float = 150.0,
) -> list[dict[str, Any]]:
    """Named recap selectors for A/B tests. GAME_END is not special except in current."""
    name = str(selector or "current").strip().lower()
    if name in {"current", "reserved", "reserved_closer"}:
        k = 5 if top_k is None else int(top_k)
        return pick_top_k(ranked, k, per_game=per_game, order=order, max_closers=1)
    if name in {"competitive", "no_reserved_closer", "top_score"}:
        k = 5 if top_k is None else int(top_k)
        return pick_top_score(ranked, k, per_game=per_game, order=order)
    if name in {"top8", "top_8"}:
        k = 8 if top_k is None else int(top_k)
        return pick_top_score(ranked, k, per_game=per_game, order=order)
    if name in {"duration", "budget", "score_floor"}:
        return pick_duration_budget(
            ranked,
            per_game=per_game,
            min_score=min_score,
            max_duration=max_duration,
            order=order,
        )
    raise ValueError(
        f"unknown selector {selector!r} (expected {', '.join(SELECTORS)})"
    )


def _select_slate(
    ranked: list[dict[str, Any]],
    k: int,
    *,
    max_per_game: int,
    max_closers: int,
    max_death_per_game: int,
    max_per_matchup: int,
    closer_needs_kill: bool,
) -> list[dict[str, Any]]:
    picked: list[dict[str, Any]] = []
    picked_paths: set[str] = set()
    game_n: dict[str, int] = {}
    game_deaths: dict[str, int] = {}
    matchup_n: dict[str, int] = {}
    n_closers = 0

    def can_take(clip: dict[str, Any], *, as_closer: bool) -> bool:
        rel = str(clip.get("relativePath") or "")
        if not rel or rel in picked_paths:
            return False
        gk = game_key(clip)
        if max_per_game > 0 and game_n.get(gk, 0) >= max_per_game:
            return False
        mk = matchup_key(clip)
        if max_per_matchup > 0 and matchup_n.get(mk, 0) >= max_per_matchup:
            return False
        types = clip.get("types") or []
        if "DEATH" in types and max_death_per_game > 0:
            if game_deaths.get(gk, 0) >= max_death_per_game and not clip.get("firstKill"):
                return False
        if as_closer:
            if max_closers <= 0 or n_closers >= max_closers:
                return False
            if closer_needs_kill and "KILL" not in types:
                return False
        return True

    def take(clip: dict[str, Any], *, as_closer: bool) -> bool:
        nonlocal n_closers
        if not can_take(clip, as_closer=as_closer):
            return False
        picked.append(clip)
        rel = str(clip.get("relativePath") or "")
        picked_paths.add(rel)
        gk = game_key(clip)
        game_n[gk] = game_n.get(gk, 0) + 1
        mk = matchup_key(clip)
        matchup_n[mk] = matchup_n.get(mk, 0) + 1
        if "DEATH" in (clip.get("types") or []):
            game_deaths[gk] = game_deaths.get(gk, 0) + 1
        if as_closer:
            n_closers += 1
        return True

    closers = [
        c
        for c in ranked
        if "GAME_END" in (c.get("types") or [])
        and (not closer_needs_kill or "KILL" in (c.get("types") or []))
    ]
    closers.sort(key=lambda c: float(c.get("closerScore") or 0.0), reverse=True)

    highlight_budget = k
    if max_closers > 0 and closers:
        highlight_budget = max(0, k - 1)

    for clip in _opener_clips(ranked):
        if len(picked) >= highlight_budget:
            break
        if "GAME_END" in (clip.get("types") or []):
            continue
        take(clip, as_closer=False)

    for clip in ranked:
        if len(picked) >= highlight_budget:
            break
        if "GAME_END" in (clip.get("types") or []):
            continue
        take(clip, as_closer=False)

    for clip in closers:
        if n_closers >= max(0, max_closers):
            break
        if len(picked) >= k:
            break
        take(clip, as_closer=True)

    if len(picked) < k:
        for clip in ranked:
            if len(picked) >= k:
                break
            if "GAME_END" in (clip.get("types") or []):
                continue
            take(clip, as_closer=False)
    return picked


def write_rank_outputs(
    *,
    dataset_dir: Path,
    clips_dir: Path,
    scored: list[dict[str, Any]],
    picks: list[dict[str, Any]],
    top_k: int,
    extra_dirs: list[Path] | None = None,
    per_game: bool = True,
    max_per_game: int = 0,
    scope: str = "game",
) -> dict[str, Path]:
    scores_payload = {
        "schema_version": 3,
        "dataset_id": dataset_dir.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "clips_dir": str(clips_dir),
        "top_k": top_k,
        "per_game": per_game,
        "max_per_game": max_per_game,
        "scope": scope,
        "weights": {
            "type": TYPE_WEIGHTS,
            "laneKill": LANE_KILL,
            "laneTrade": LANE_TRADE,
            "assistOnly": ASSIST_ONLY,
            "firstKill": FIRST_KILL,
            "firstLaneSolo": FIRST_LANE_SOLO,
            "voiceCap": VOICE_CAP,
        },
        "clip_count": len(scored),
        "picked_count": len(picks),
        "clips": scored,
    }
    picks_payload = {
        "schema_version": 3,
        "dataset_id": dataset_dir.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "top_k": top_k,
        "per_game": per_game,
        "max_per_game": max_per_game,
        "scope": scope,
        "picked_seconds": round(sum(float(c.get("duration") or 0.0) for c in picks), 3),
        "clip_count": len(picks),
        "clips": [
            {
                "index": i,
                "rank": c.get("rank"),
                "score": c.get("score"),
                "relativePath": c["relativePath"],
                "filename": c.get("filename"),
                "gameFolder": c.get("gameFolder"),
                "vodId": c.get("vodId"),
                "gameIndex": c.get("gameIndex"),
                "clipIndexInGame": c.get("clipIndexInGame"),
                "champion": c.get("champion"),
                "laneOpponentChampion": c.get("laneOpponentChampion"),
                "opponentChampion": c.get("opponentChampion"),
                "types": c.get("types"),
                "duration": c.get("duration"),
                "why": c.get("why"),
                "closerScore": c.get("closerScore"),
            }
            for i, c in enumerate(picks, start=1)
        ],
    }
    written: dict[str, Path] = {}
    dirs = [clips_dir, *(extra_dirs or [])]
    for dest in dirs:
        dest.mkdir(parents=True, exist_ok=True)
        scores_path = dest / "clip_scores.json"
        picks_path = dest / "top_picks.json"
        scores_path.write_text(
            json.dumps(scores_payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        picks_path.write_text(
            json.dumps(picks_payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        written[str(dest)] = picks_path
    return written


def pick_daily(
    ranked: list[dict[str, Any]],
    *,
    target_seconds: float,
    max_per_game: int,
    max_assist_only: int,
) -> list[dict[str, Any]]:
    picked: list[dict[str, Any]] = []
    used = 0.0
    per_game: dict[int, int] = {}
    assists = 0

    def can_take(clip: dict[str, Any]) -> bool:
        g = int(clip.get("gameIndex") or 0)
        if per_game.get(g, 0) >= max_per_game:
            return False
        types = set(clip.get("types") or [])
        if types and types <= {"ASSIST"}:
            if assists >= max_assist_only:
                return False
        dur = float(clip.get("duration") or 0.0)
        if picked and used + dur > target_seconds * 1.15:
            return False
        return True

    for clip in ranked:
        if used >= target_seconds:
            break
        if not can_take(clip):
            continue
        picked.append(clip)
        used += float(clip.get("duration") or 0.0)
        g = int(clip.get("gameIndex") or 0)
        per_game[g] = per_game.get(g, 0) + 1
        if set(clip.get("types") or []) <= {"ASSIST"}:
            assists += 1

    # Guarantee a closer if we ranked one in the top half and didn't pick it.
    closers = [c for c in ranked if "GAME_END" in (c.get("types") or [])]
    if closers:
        best_end = closers[0]
        if best_end["relativePath"] not in {c["relativePath"] for c in picked}:
            if can_take(best_end) or len(picked) < 2:
                picked.append(best_end)

    picked.sort(
        key=lambda c: (
            int(c.get("gameIndex") or 0),
            int(c.get("clipIndexInGame") or 0),
        )
    )
    return picked


def preview_row(clip: dict[str, Any]) -> dict[str, Any]:
    return {
        "rank": clip.get("rank"),
        "score": clip.get("score"),
        "file": clip.get("relativePath"),
        "types": clip.get("types"),
        "vsLane": clip.get("vsLane"),
        "duration": clip.get("duration"),
        "why": clip.get("why"),
        "components": clip.get("components"),
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Rank LoL clips and keep the top K (per game or across the day)."
    )
    p.add_argument("--dataset-id")
    p.add_argument("--dataset-dir", type=Path)
    p.add_argument("--output-root", type=Path, default=Path("data"))
    p.add_argument("--clips-dir", type=Path)
    p.add_argument(
        "--top-k",
        type=int,
        default=None,
        help="Keep the best N clips (default: 5 per game, 12 with --daily).",
    )
    p.add_argument(
        "--daily",
        action="store_true",
        help="Rank globally across every game/VOD under clips-dir (one daily slate).",
    )
    p.add_argument(
        "--max-per-game",
        type=int,
        default=3,
        help="With --daily, max clips from one game (default: 3). 0 = no cap.",
    )
    p.add_argument(
        "--max-per-matchup",
        type=int,
        default=2,
        help="With --daily, max clips of the same champ vs same lane opponent.",
    )
    p.add_argument(
        "--max-death-per-game",
        type=int,
        default=1,
        help="With --daily, max clips that include a death from one game.",
    )
    p.add_argument(
        "--max-closers",
        type=int,
        default=1,
        help="Max GAME_END clips in the slate (default: 1). 0 = none.",
    )
    p.add_argument(
        "--order",
        choices=("chrono", "score"),
        default="chrono",
        help="Stitch/pick order (default: chrono through the day).",
    )
    p.add_argument(
        "--target-seconds",
        type=float,
        default=0.0,
        help="Optional duration cap after top-K (0 = off).",
    )
    p.add_argument("--max-assist-only", type=int, default=1)
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.dataset_dir is not None:
        dataset_dir = args.dataset_dir.resolve()
    elif args.dataset_id:
        found = find_dataset_dir(args.output_root, args.dataset_id)
        dataset_dir = (found or args.output_root / args.dataset_id).resolve()
    else:
        print("Provide --dataset-id or --dataset-dir.", file=sys.stderr)
        return 2

    clips_dir = (args.clips_dir or dataset_dir / "lol_clips").resolve()
    if not clips_dir.is_dir():
        print(f"Missing clips dir: {clips_dir}", file=sys.stderr)
        return 1

    out_scores = clips_dir / "clip_scores.json"
    if out_scores.exists() and not args.force:
        print(f"{out_scores.name} already exists. Pass --force to overwrite.", file=sys.stderr)
        return 2

    scored = rank_dataset(dataset_dir, clips_dir)
    if not scored:
        print(f"No clips in {clips_dir}", file=sys.stderr)
        return 1

    daily = bool(args.daily)
    k = int(args.top_k) if args.top_k is not None else (12 if daily else 5)
    picks = pick_top_k(
        scored,
        k,
        per_game=not daily,
        max_per_game=int(args.max_per_game) if daily else 0,
        order=str(args.order),
        max_closers=int(args.max_closers),
        max_death_per_game=int(args.max_death_per_game) if daily else 2,
        max_per_matchup=int(args.max_per_matchup) if daily else 0,
        closer_needs_kill=daily,
    )
    if float(args.target_seconds) > 0:
        picks = pick_daily(
            picks,
            target_seconds=float(args.target_seconds),
            max_per_game=int(args.max_per_game) if daily else (k if k > 0 else 99),
            max_assist_only=int(args.max_assist_only),
        )
    pick_set = {c["relativePath"] for c in picks}
    write_rank_outputs(
        dataset_dir=dataset_dir,
        clips_dir=clips_dir,
        scored=scored,
        picks=picks,
        top_k=k,
        per_game=not daily,
        max_per_game=int(args.max_per_game) if daily else 0,
        scope="daily" if daily else "game",
    )

    print(
        json.dumps(
            {
                "status": "ok",
                "scores": str(clips_dir / "clip_scores.json"),
                "picks": str(clips_dir / "top_picks.json"),
                "clip_count": len(scored),
                "scope": "daily" if daily else "game",
                "top_k": k,
                "picked_count": len(picks),
                "picked_seconds": round(
                    sum(float(c.get("duration") or 0.0) for c in picks), 3
                ),
                "top": [preview_row(c) for c in scored[: max(k, 10)]],
                "kept": [c["relativePath"] for c in picks],
                "dropped": [
                    c["relativePath"]
                    for c in scored
                    if c["relativePath"] not in pick_set
                ],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
