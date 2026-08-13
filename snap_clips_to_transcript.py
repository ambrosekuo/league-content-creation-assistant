#!/usr/bin/env python3
"""Snap LoL clip windows to nearby transcript segment boundaries."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Missing file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def format_hms(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def segment_covering(segments: list[dict[str, Any]], t: float) -> dict[str, Any] | None:
    for seg in segments:
        if float(seg["start"]) <= t <= float(seg["end"]):
            return seg
    return None


def previous_segment(segments: list[dict[str, Any]], t: float) -> dict[str, Any] | None:
    prev = None
    for seg in segments:
        if float(seg["end"]) <= t:
            prev = seg
        elif float(seg["start"]) > t:
            break
    return prev


def next_segment(segments: list[dict[str, Any]], t: float) -> dict[str, Any] | None:
    for seg in segments:
        if float(seg["start"]) >= t:
            return seg
    return None


def snap_start(
    t: float,
    *,
    segments: list[dict[str, Any]],
    event_t: float,
    max_expand: float,
    min_gap: float,
) -> tuple[float, str]:
    """Snap a proposed start time backward onto a clean speech boundary."""
    floor = max(0.0, event_t - max_expand - 15.0)  # absolute floor handled by caller too
    covering = segment_covering(segments, t)
    if covering is not None:
        candidate = float(covering["start"])
        if event_t - candidate <= max_expand + 15.0 and candidate <= event_t:
            return max(0.0, candidate), "segment_start"
        # Segment too long to fully include — keep original.
        return max(0.0, t), "keep_mid_segment"

    # In a gap: optionally pull back to previous segment start if the gap is small
    # and previous speech is close (keeps a full preceding thought).
    prev = previous_segment(segments, t)
    if prev is not None:
        gap = t - float(prev["end"])
        if 0 <= gap < min_gap:
            # Tiny gap / punctuation pause — stay at proposed t (already in silence).
            return max(0.0, t), "silence_gap"
        # If we're just after a segment ended, starting in silence is ideal.
        if gap >= min_gap:
            return max(0.0, t), "silence_gap"
    return max(0.0, t), "unchanged"


def snap_end(
    t: float,
    *,
    segments: list[dict[str, Any]],
    event_t: float,
    max_expand: float,
    min_gap: float,
) -> tuple[float, str]:
    """Snap a proposed end time forward onto a clean speech boundary."""
    covering = segment_covering(segments, t)
    if covering is not None:
        candidate = float(covering["end"])
        if candidate - event_t <= max_expand + 10.0 and candidate >= event_t:
            return candidate, "segment_end"
        return t, "keep_mid_segment"

    nxt = next_segment(segments, t)
    if nxt is not None:
        gap = float(nxt["start"]) - t
        if 0 <= gap < min_gap:
            return t, "silence_gap"
    return t, "unchanged"


def refine_window(
    event_t: float,
    *,
    segments: list[dict[str, Any]],
    pre_roll: float,
    post_roll: float,
    max_expand: float,
    max_duration: float,
    min_gap: float,
) -> dict[str, Any]:
    raw_start = max(0.0, event_t - pre_roll)
    raw_end = max(raw_start + 0.1, event_t + post_roll)

    # Allow expand only within [event - pre - max_expand, event + post + max_expand]
    earliest = max(0.0, event_t - pre_roll - max_expand)
    latest = event_t + post_roll + max_expand

    start, start_reason = snap_start(
        raw_start,
        segments=segments,
        event_t=event_t,
        max_expand=max_expand,
        min_gap=min_gap,
    )
    end, end_reason = snap_end(
        raw_end,
        segments=segments,
        event_t=event_t,
        max_expand=max_expand,
        min_gap=min_gap,
    )

    start = max(earliest, min(start, event_t))
    end = min(latest, max(end, event_t + 0.1))

    # If still mid-sentence at start, force to covering segment start when in bounds.
    cover_s = segment_covering(segments, start)
    if cover_s is not None:
        seg_start = float(cover_s["start"])
        if seg_start >= earliest and seg_start <= event_t:
            start = seg_start
            start_reason = "segment_start_forced"

    cover_e = segment_covering(segments, end)
    if cover_e is not None:
        seg_end = float(cover_e["end"])
        if seg_end <= latest and seg_end >= event_t:
            end = seg_end
            end_reason = "segment_end_forced"

    if end - start > max_duration:
        # Keep the event, trim the side that expanded more.
        overflow = (end - start) - max_duration
        expand_left = max(0.0, (event_t - pre_roll) - start)
        expand_right = max(0.0, end - (event_t + post_roll))
        if expand_left >= expand_right:
            start = min(event_t, start + overflow)
            start_reason = f"{start_reason}+trim_to_max"
        else:
            end = max(event_t + 0.1, end - overflow)
            end_reason = f"{end_reason}+trim_to_max"

    # Collect overlapping transcript for context.
    texts = [
        s["text"]
        for s in segments
        if not (float(s["end"]) < start or float(s["start"]) > end)
    ]

    return {
        "start": round(start, 3),
        "end": round(end, 3),
        "rawStart": round(raw_start, 3),
        "rawEnd": round(raw_end, 3),
        "startReason": start_reason,
        "endReason": end_reason,
        "clipStart": format_hms(start),
        "clipEnd": format_hms(end),
        "transcript": " ".join(texts).strip(),
    }


def tighten_to_speech(
    window: dict[str, Any],
    *,
    segments: list[dict[str, Any]],
    event_t: float,
    pad: float = 0.4,
) -> dict[str, Any]:
    """
    Start from a wide window; shrink to the span of overlapping speech.
    Always keeps a small pad around the kill/death instant.
    """
    start = float(window["start"])
    end = float(window["end"])
    raw_start = float(window["rawStart"])
    raw_end = float(window["rawEnd"])
    overlapping = [
        s
        for s in segments
        if not (float(s["end"]) < start or float(s["start"]) > end)
    ]
    if not overlapping:
        return window

    speech_start = min(float(s["start"]) for s in overlapping)
    speech_end = max(float(s["end"]) for s in overlapping)
    # Clamp to the original wide search window; keep event visible.
    new_start = max(raw_start, min(speech_start - pad, event_t - pad))
    new_end = min(raw_end, max(speech_end + pad, event_t + pad))
    if new_end <= new_start + 0.1:
        return window

    texts = [
        s["text"]
        for s in segments
        if not (float(s["end"]) < new_start or float(s["start"]) > new_end)
    ]
    out = dict(window)
    out.update(
        {
            "start": round(new_start, 3),
            "end": round(new_end, 3),
            "clipStart": format_hms(new_start),
            "clipEnd": format_hms(new_end),
            "startReason": f"{window.get('startReason')}+tighten_speech",
            "endReason": f"{window.get('endReason')}+tighten_speech",
            "transcript": " ".join(texts).strip(),
            "wideStart": round(start, 3),
            "wideEnd": round(end, 3),
        }
    )
    return out


def _transcript_for_span(
    segments: list[dict[str, Any]], start: float, end: float
) -> str:
    texts = [
        s["text"]
        for s in segments
        if not (float(s["end"]) < start or float(s["start"]) > end)
    ]
    return " ".join(texts).strip()


def center_on_event(
    event_t: float,
    *,
    segments: list[dict[str, Any]],
    pre_roll: float,
    post_roll: float,
    max_duration: float,
    speech_nudge: float = 2.0,
) -> dict[str, Any]:
    """
    Short window centered on the kill/death.

    Transcript is used only for a small boundary nudge (±speech_nudge) so we
    avoid chopping mid-word when a segment edge is nearby — not to keep long
    speech spans.
    """
    raw_start = max(0.0, event_t - pre_roll)
    raw_end = max(raw_start + 0.1, event_t + post_roll)
    start, start_reason = raw_start, "center_pre"
    end, end_reason = raw_end, "center_post"

    if speech_nudge > 0:
        cover_s = segment_covering(segments, start)
        if cover_s is not None:
            seg_start = float(cover_s["start"])
            if 0 < (start - seg_start) <= speech_nudge and seg_start <= event_t:
                start = max(0.0, seg_start)
                start_reason = "center_pre+speech_nudge"
        cover_e = segment_covering(segments, end)
        if cover_e is not None:
            seg_end = float(cover_e["end"])
            if 0 < (seg_end - end) <= speech_nudge and seg_end >= event_t:
                end = seg_end
                end_reason = "center_post+speech_nudge"

    if end - start > max_duration:
        # Keep pre/post ratio; always keep the event inside the window.
        total = max(pre_roll + post_roll, 0.1)
        pre_budget = min(pre_roll, max_duration * (pre_roll / total))
        post_budget = max_duration - pre_budget
        start = max(0.0, event_t - pre_budget)
        end = event_t + post_budget
        if end <= start + 0.1:
            end = start + 0.1
        start_reason = f"{start_reason}+trim_to_max"
        end_reason = f"{end_reason}+trim_to_max"

    return {
        "start": round(start, 3),
        "end": round(end, 3),
        "rawStart": round(raw_start, 3),
        "rawEnd": round(raw_end, 3),
        "startReason": start_reason,
        "endReason": end_reason,
        "clipStart": format_hms(start),
        "clipEnd": format_hms(end),
        "transcript": _transcript_for_span(segments, start, end),
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Snap LoL KILL/DEATH clip windows to transcript boundaries."
    )
    p.add_argument("--dataset-dir", type=Path, required=True)
    p.add_argument("--events", type=Path, default=None, help="lol_events.json path")
    p.add_argument("--transcript", type=Path, default=None, help="transcript.json path")
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output JSON (default: <dataset>/lol_events_snapped.json)",
    )
    p.add_argument("--types", default="KILL,DEATH,ASSIST")
    p.add_argument(
        "--pre-roll",
        type=float,
        default=8.0,
        help="Seconds before event (default: 8; center-on-event mode)",
    )
    p.add_argument(
        "--post-roll",
        type=float,
        default=10.0,
        help="Seconds after event (default: 10; center-on-event mode)",
    )
    p.add_argument("--max-expand", type=float, default=8.0)
    p.add_argument(
        "--max-duration",
        type=float,
        default=22.0,
        help="Hard max clip length in seconds (default: 22)",
    )
    p.add_argument(
        "--min-gap",
        type=float,
        default=0.3,
        help="Treat gaps >= this many seconds as natural cut points",
    )
    p.add_argument(
        "--merge-gap",
        type=float,
        default=0.0,
        help="Merge nearby windows within N seconds (default: 0 = no gap-merge)",
    )
    p.add_argument(
        "--overlap-merge",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Merge overlapping/near-touching windows (default: on)",
    )
    p.add_argument(
        "--overlap-slack",
        type=float,
        default=1.0,
        help="With overlap-merge, treat gaps <= N seconds as overlap (default: 1)",
    )
    p.add_argument(
        "--center-on-event",
        action="store_true",
        help="Short window centered on kill/death with soft speech nudge",
    )
    p.add_argument(
        "--speech-nudge",
        type=float,
        default=2.0,
        help="With --center-on-event, max seconds to nudge to word edges (default: 2)",
    )
    p.add_argument(
        "--tighten-to-speech",
        action="store_true",
        help="After wide snap, shrink window to overlapping transcript span",
    )
    p.add_argument(
        "--game-end-duration",
        type=float,
        default=13.0,
        help=(
            "Standalone nexus/GAME_END clip length in seconds (default: 13). "
            "If the last KDA falls in this window, that clip is extended instead. "
            "Set 0 to disable."
        ),
    )
    p.add_argument(
        "--game-end-tail",
        type=float,
        default=5.0,
        help="Seconds after GAME_END for explosion/banner (default: 5)",
    )
    return p


def merge_nearby(items: list[dict[str, Any]], merge_gap: float) -> list[dict[str, Any]]:
    if not items or merge_gap <= 0:
        return items
    items = sorted(items, key=lambda x: x["start"])
    merged: list[dict[str, Any]] = []
    cur = dict(items[0])
    cur["types"] = [cur["type"]]
    cur["sources"] = [items[0]]
    for item in items[1:]:
        if item["start"] <= cur["end"] + merge_gap:
            cur["end"] = max(cur["end"], item["end"])
            cur["types"].append(item["type"])
            cur["sources"].append(item)
            # Prefer earliest event time label.
        else:
            merged.append(cur)
            cur = dict(item)
            cur["types"] = [cur["type"]]
            cur["sources"] = [item]
    merged.append(cur)
    return merged


def merge_overlapping_windows(
    items: list[dict[str, Any]],
    *,
    slack: float = 1.0,
) -> list[dict[str, Any]]:
    """
    Collapse windows that overlap or nearly touch.

    Stops near-duplicate clips when kills/deaths land a few seconds apart
    (same fight, almost the same footage).
    """
    if not items:
        return items
    items = sorted(items, key=lambda x: (float(x["start"]), float(x["end"])))
    merged: list[dict[str, Any]] = []
    cur = dict(items[0])
    cur["types"] = list(cur.get("types") or [cur["type"]])
    cur["sources"] = list(cur.get("sources") or [items[0]])
    for item in items[1:]:
        if float(item["start"]) <= float(cur["end"]) + slack:
            cur["end"] = max(float(cur["end"]), float(item["end"]))
            cur["start"] = min(float(cur["start"]), float(item["start"]))
            cur["types"].extend(item.get("types") or [item["type"]])
            cur["sources"].extend(item.get("sources") or [item])
            # Keep earliest primary event metadata for labeling.
            if float(item.get("vodOffsetSeconds") or 1e18) < float(
                cur.get("vodOffsetSeconds") or 1e18
            ):
                for key in (
                    "type",
                    "vodOffsetSeconds",
                    "vodTime",
                    "gameTime",
                    "opponentChampion",
                    "matchId",
                    "champion",
                ):
                    if key in item:
                        cur[key] = item[key]
            cur["startReason"] = f"{cur.get('startReason')}+overlap_merge"
            cur["endReason"] = f"{cur.get('endReason')}+overlap_merge"
            cur["clipStart"] = format_hms(float(cur["start"]))
            cur["clipEnd"] = format_hms(float(cur["end"]))
        else:
            merged.append(cur)
            cur = dict(item)
            cur["types"] = list(item.get("types") or [item["type"]])
            cur["sources"] = list(item.get("sources") or [item])
    merged.append(cur)
    return merged


def _event_time_span(window: dict[str, Any]) -> tuple[float, float]:
    """First/last action times inside a (possibly merged) window."""
    times: list[float] = []

    def _collect(node: dict[str, Any]) -> None:
        t = node.get("vodOffsetSeconds")
        if t is not None:
            times.append(float(t))
        for src in node.get("sources") or []:
            if isinstance(src, dict):
                _collect(src)

    _collect(window)
    if not times:
        start = float(window.get("start") or 0.0)
        end = float(window.get("end") or start)
        mid = (start + end) / 2.0
        return mid, mid
    return min(times), max(times)


def trim_window_to_max(
    window: dict[str, Any],
    *,
    pre_roll: float,
    post_roll: float,
    max_duration: float,
) -> dict[str, Any]:
    """
    Cap duration while keeping:
      - post-roll after the LAST action
      - pre-roll before the FIRST action (chain kills must not lose opener buffer)

    Merged multi-kill clips used to re-center on the first kill's timestamp,
    which chopped the ending right after the first kill. The later fix of
    ``trim_keep_last_action`` then stole the first kill's lead-in whenever the
    chain exceeded ``max_duration``. For chains we keep both anchors and allow
    the window to exceed ``max_duration``.
    """
    first_t, last_t = _event_time_span(window)
    window["firstEventOffset"] = round(first_t, 3)
    window["lastEventOffset"] = round(last_t, 3)

    start = float(window["start"])
    end = float(window["end"])
    ideal_start = max(0.0, first_t - pre_roll)
    ideal_end = last_t + post_roll
    start = min(start, ideal_start)
    end = max(end, ideal_end)

    source_count = len(window.get("sources") or [])
    if source_count <= 1:
        # Also count nested/types for overlap-merged windows that flattened sources.
        source_count = max(
            1,
            int(window.get("source_event_count") or 0),
            len(window.get("types") or []),
        )
    is_chain = source_count > 1 or (last_t - first_t) > 1.0

    if end - start > max_duration:
        # Always keep last-action post-roll + first-action pre-roll.
        end = last_t + post_roll
        start = max(0.0, first_t - pre_roll)
        if end - start > max_duration and not is_chain:
            # Single action: hard-cap by trimming lead-in only.
            start = max(0.0, end - max_duration)
            window["endReason"] = f"{window.get('endReason')}+trim_keep_last_action"
            if start > last_t:
                start = max(0.0, last_t)
                end = start + max_duration
        elif end - start > max_duration:
            # Chain / multi-kill: keep both buffers; allow over max_duration.
            window["endReason"] = f"{window.get('endReason')}+chain_keep_first_pre"
            window["overMaxDuration"] = True

    window["start"] = round(start, 3)
    window["end"] = round(end, 3)
    window["clipStart"] = format_hms(start)
    window["clipEnd"] = format_hms(end)
    return window


def _game_end_offsets(events_payload: dict[str, Any]) -> dict[str, float]:
    """matchId → GAME_END vodOffsetSeconds."""
    out: dict[str, float] = {}
    for match in events_payload.get("matches") or []:
        mid = match.get("matchId")
        if not mid:
            continue
        for event in match.get("events") or []:
            if event.get("type") != "GAME_END":
                continue
            if event.get("vodOffsetSeconds") is None:
                continue
            out[str(mid)] = float(event["vodOffsetSeconds"])
            break
    return out


def apply_game_end_bookends(
    windows: list[dict[str, Any]],
    events_payload: dict[str, Any],
    *,
    end_duration: float = 13.0,
    end_tail: float = 5.0,
    segments: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """
    Attach nexus / GAME_END coverage.

    If the last KILL/DEATH/ASSIST for a match falls inside the end window
    (or the last clip already overlaps it), extend that clip through
    GAME_END + tail — no separate end segment to stitch.

    Otherwise add a standalone ~end_duration GAME_END window.
    """
    if end_duration <= 0:
        return windows

    ends = _game_end_offsets(events_payload)
    if not ends:
        return windows

    pre = max(0.1, float(end_duration) - float(end_tail))
    post = max(0.0, float(end_tail))
    segs = segments or []

    by_match: dict[str, list[dict[str, Any]]] = {}
    for w in windows:
        mid = str(w.get("matchId") or "")
        if mid:
            by_match.setdefault(mid, []).append(w)

    extras: list[dict[str, Any]] = []
    for mid, game_end_t in ends.items():
        if game_end_t < 0:
            # Ended before VOD start — nothing to cut.
            continue
        end_start = max(0.0, game_end_t - pre)
        end_end = game_end_t + post
        match_windows = by_match.get(mid) or []

        last_action: float | None = None
        last_window: dict[str, Any] | None = None
        for w in match_windows:
            _first, last_t = _event_time_span(w)
            if last_action is None or last_t >= last_action:
                last_action = last_t
                last_window = w

        overlaps = False
        if last_window is not None and last_action is not None:
            # Last KDA is inside the end window, or last clip already reaches it.
            if last_action >= end_start or float(last_window["end"]) >= end_start:
                overlaps = True

        if overlaps and last_window is not None:
            new_end = max(float(last_window["end"]), end_end)
            last_window["end"] = round(new_end, 3)
            last_window["clipEnd"] = format_hms(new_end)
            last_window["duration"] = round(new_end - float(last_window["start"]), 3)
            types = list(last_window.get("types") or [last_window.get("type")])
            if "GAME_END" not in types:
                types.append("GAME_END")
            last_window["types"] = types
            last_window["includesGameEnd"] = True
            last_window["gameEndOffset"] = round(game_end_t, 3)
            last_window["endReason"] = f"{last_window.get('endReason')}+extend_to_game_end"
            if segs:
                last_window["transcript"] = _transcript_for_span(
                    segs, float(last_window["start"]), new_end
                )
            print(
                f"[snap] {mid}: last KDA overlaps end window — "
                f"extend clip through GAME_END ({game_end_t:.1f}s)",
                flush=True,
            )
            continue

        # Standalone nexus clip (~10s by default).
        meta = next(
            (m for m in (events_payload.get("matches") or []) if str(m.get("matchId")) == mid),
            {},
        )
        end_event = next(
            (
                e
                for e in (meta.get("events") or [])
                if e.get("type") == "GAME_END"
            ),
            {},
        )
        extras.append(
            {
                "type": "GAME_END",
                "matchId": mid,
                "champion": meta.get("champion"),
                "win": meta.get("win"),
                "queueId": meta.get("queueId"),
                "teamPosition": meta.get("teamPosition"),
                "laneOpponentChampion": meta.get("laneOpponentChampion"),
                "opponentChampion": end_event.get("opponentChampion"),
                "gameTime": end_event.get("gameTime"),
                "vodTime": end_event.get("vodTime"),
                "vodOffsetSeconds": game_end_t,
                "firstEventOffset": round(game_end_t, 3),
                "lastEventOffset": round(game_end_t, 3),
                "start": round(end_start, 3),
                "end": round(end_end, 3),
                "clipStart": format_hms(end_start),
                "clipEnd": format_hms(end_end),
                "rawStart": round(end_start, 3),
                "rawEnd": round(end_end, 3),
                "startReason": "game_end_pre",
                "endReason": "game_end_post",
                "duration": round(end_end - end_start, 3),
                "types": ["GAME_END"],
                "includesGameEnd": True,
                "gameEndOffset": round(game_end_t, 3),
                "standaloneGameEnd": True,
                "transcript": _transcript_for_span(segs, end_start, end_end) if segs else "",
                "sources": [],
            }
        )
        print(
            f"[snap] {mid}: standalone GAME_END window "
            f"{format_hms(end_start)}–{format_hms(end_end)} ({end_end - end_start:.1f}s)",
            flush=True,
        )

    windows.extend(extras)
    windows.sort(key=lambda w: float(w.get("start") or 0.0))
    return windows


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dataset_dir = args.dataset_dir.resolve()
    events_path = (args.events or dataset_dir / "lol_events.json").resolve()
    transcript_path = (args.transcript or dataset_dir / "transcript.json").resolve()
    output_path = (args.output or dataset_dir / "lol_events_snapped.json").resolve()

    try:
        events_payload = load_json(events_path)
        transcript_payload = load_json(transcript_path)
        segments = list(transcript_payload.get("segments") or [])
        segments.sort(key=lambda s: float(s["start"]))
        types = {t.strip().upper() for t in args.types.split(",") if t.strip()}

        refined: list[dict[str, Any]] = []
        for match in events_payload.get("matches") or []:
            for event in match.get("events") or []:
                if event.get("type") not in types:
                    continue
                if event.get("vodOffsetSeconds") is None:
                    continue
                event_t = float(event["vodOffsetSeconds"])
                if args.center_on_event:
                    window = center_on_event(
                        event_t,
                        segments=segments,
                        pre_roll=args.pre_roll,
                        post_roll=args.post_roll,
                        max_duration=args.max_duration,
                        speech_nudge=args.speech_nudge,
                    )
                else:
                    window = refine_window(
                        event_t,
                        segments=segments,
                        pre_roll=args.pre_roll,
                        post_roll=args.post_roll,
                        max_expand=args.max_expand,
                        max_duration=args.max_duration,
                        min_gap=args.min_gap,
                    )
                    if args.tighten_to_speech:
                        window = tighten_to_speech(
                            window, segments=segments, event_t=event_t
                        )
                refined.append(
                    {
                        "type": event["type"],
                        "matchId": match.get("matchId"),
                        "champion": match.get("champion"),
                        "win": match.get("win"),
                        "queueId": match.get("queueId"),
                        "teamPosition": match.get("teamPosition"),
                        "laneOpponentChampion": match.get("laneOpponentChampion"),
                        "opponentChampion": event.get("opponentChampion"),
                        "gameTime": event.get("gameTime"),
                        "vodTime": event.get("vodTime"),
                        "vodOffsetSeconds": event_t,
                        "start": window["start"],
                        "end": window["end"],
                        "clipStart": window["clipStart"],
                        "clipEnd": window["clipEnd"],
                        "rawStart": window["rawStart"],
                        "rawEnd": window["rawEnd"],
                        "startReason": window["startReason"],
                        "endReason": window["endReason"],
                        "deltaStart": round(window["start"] - window["rawStart"], 3),
                        "deltaEnd": round(window["end"] - window["rawEnd"], 3),
                        "transcript": window["transcript"],
                        "wideStart": window.get("wideStart"),
                        "wideEnd": window.get("wideEnd"),
                    }
                )

        windows = merge_nearby(refined, merge_gap=args.merge_gap)
        if args.overlap_merge:
            before = len(windows)
            windows = merge_overlapping_windows(windows, slack=args.overlap_slack)
            if len(windows) != before:
                print(
                    f"[snap] overlap-merge {before} → {len(windows)} windows",
                    flush=True,
                )
        # After merge, recompute combined transcript span text lightly.
        for w in windows:
            # Cap merged fights; always keep post-roll after the LAST action.
            trim_window_to_max(
                w,
                pre_roll=args.pre_roll,
                post_roll=args.post_roll,
                max_duration=args.max_duration,
            )
            w["duration"] = round(float(w["end"]) - float(w["start"]), 3)
            w["types"] = list(dict.fromkeys(w.get("types") or [w["type"]]))
            w["clipStart"] = format_hms(float(w["start"]))
            w["clipEnd"] = format_hms(float(w["end"]))
            # Refresh transcript for final span.
            w["transcript"] = _transcript_for_span(
                segments, float(w["start"]), float(w["end"])
            )

        windows = apply_game_end_bookends(
            windows,
            events_payload,
            end_duration=float(args.game_end_duration),
            end_tail=float(args.game_end_tail),
            segments=segments,
        )
        for w in windows:
            w["duration"] = round(float(w["end"]) - float(w["start"]), 3)
            w["clipStart"] = format_hms(float(w["start"]))
            w["clipEnd"] = format_hms(float(w["end"]))

        out = {
            "schema_version": 1,
            "dataset_id": dataset_dir.name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_events": str(events_path),
            "source_transcript": str(transcript_path),
            "types": sorted(types),
            "pre_roll": args.pre_roll,
            "post_roll": args.post_roll,
            "max_expand": args.max_expand,
            "max_duration": args.max_duration,
            "merge_gap": args.merge_gap,
            "overlap_merge": bool(args.overlap_merge),
            "overlap_slack": args.overlap_slack,
            "center_on_event": bool(args.center_on_event),
            "speech_nudge": args.speech_nudge if args.center_on_event else None,
            "game_end_duration": args.game_end_duration,
            "game_end_tail": args.game_end_tail,
            "window_count": len(windows),
            "windows": windows,
        }
        output_path.write_text(
            json.dumps(out, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        changed = sum(
            1
            for w in windows
            for src in (w.get("sources") or [w])
            if abs(float(src.get("deltaStart", 0))) > 0.05
            or abs(float(src.get("deltaEnd", 0))) > 0.05
        )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "output": str(output_path),
                    "window_count": len(windows),
                    "windows_with_snap_delta": changed,
                },
                indent=2,
            )
        )
        return 0
    except Exception as exc:
        print(f"Snap failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
