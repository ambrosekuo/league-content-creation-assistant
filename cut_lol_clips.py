#!/usr/bin/env python3
"""Cut LoL KDA clips from a VOD using lol_events.json timestamps."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_TYPES = ("KILL", "DEATH", "ASSIST")


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=True, text=True, capture_output=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required executable was not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise RuntimeError(f"Command failed: {' '.join(command)}\n{message}") from exc


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Missing file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def parse_hms(value: str) -> float:
    parts = value.split(":")
    if len(parts) == 2:
        minutes, seconds = parts
        return int(minutes) * 60 + int(seconds)
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return int(hours) * 3600 + int(minutes) * 60 + int(seconds)
    raise ValueError(f"Invalid time value: {value}")


def format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}h{minutes:02d}m{secs:02d}s"


def safe_token(value: str | None, *, fallback: str = "Unknown") -> str:
    text = "".join(ch for ch in str(value or "") if ch.isalnum())
    return text or fallback


def assign_game_clip_indices(windows: list[dict[str, Any]]) -> None:
    """Number games in VOD order; number clips within each game."""
    first_seen: dict[str, float] = {}
    for window in windows:
        match_id = str(window.get("matchId") or "unknown")
        start = float(window.get("start") or 0.0)
        if match_id not in first_seen or start < first_seen[match_id]:
            first_seen[match_id] = start
    order = sorted(first_seen.keys(), key=lambda mid: first_seen[mid])
    game_index = {mid: i + 1 for i, mid in enumerate(order)}
    counters: dict[str, int] = {}
    for window in windows:
        match_id = str(window.get("matchId") or "unknown")
        counters[match_id] = counters.get(match_id, 0) + 1
        window["gameIndex"] = game_index[match_id]
        window["clipIndexInGame"] = counters[match_id]


def clip_relpath(window: dict[str, Any]) -> Path:
    """
    Per-game folder + clip number for easy browse/stitch:

      g01_Leblanc/c03_kill.mp4
      g01_Leblanc/c03_kill_vsAhri.mp4   (when opponent known)
    """
    champ = safe_token(window.get("champion"))
    game_n = int(window.get("gameIndex") or 1)
    clip_n = int(window.get("clipIndexInGame") or 1)
    label = "+".join(
        dict.fromkeys(t.lower() for t in (window.get("types") or [window.get("type") or "event"]))
    )
    folder = f"g{game_n:02d}_{champ}"
    stem = f"c{clip_n:02d}_{label}"
    opponent = window.get("opponentChampion")
    if opponent:
        stem = f"{stem}_vs{safe_token(opponent)}"
    return Path(folder) / f"{stem}.mp4"


def find_source(dataset_dir: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        path = explicit.resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Source media not found: {path}")
        return path

    ingest_path = dataset_dir / "ingest.json"
    if ingest_path.is_file():
        ingest = load_json(ingest_path)
        source = Path(ingest.get("source_path") or "")
        if source.is_file():
            return source

    candidates = sorted(
        [
            p
            for p in dataset_dir.iterdir()
            if p.is_file()
            and p.suffix.lower() in {".mp4", ".mkv", ".mov", ".webm"}
            and not p.name.endswith(".part")
        ],
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            f"No source video in {dataset_dir}. Run ingest_vod.py first, or pass --source."
        )
    return candidates[0]


def collect_events(
    payload: dict[str, Any],
    types: set[str],
) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    for match in payload.get("matches") or []:
        for event in match.get("events") or []:
            event_type = event.get("type")
            if event_type not in types:
                continue
            if event.get("vodOffsetSeconds") is None and not event.get("clipStart"):
                continue

            if event.get("clipStart") is not None and event.get("clipEnd") is not None:
                start = parse_hms(str(event["clipStart"]))
                end = parse_hms(str(event["clipEnd"]))
            else:
                # Fallback: center a short window on the event offset.
                center = float(event["vodOffsetSeconds"])
                start = max(0.0, center - 15.0)
                end = max(start + 0.1, center + 10.0)

            collected.append(
                {
                    "type": event_type,
                    "matchId": match.get("matchId"),
                    "champion": match.get("champion"),
                    "win": match.get("win"),
                    "queueId": match.get("queueId"),
                    "teamPosition": match.get("teamPosition"),
                    "opponentChampion": event.get("opponentChampion"),
                    "gameTime": event.get("gameTime"),
                    "vodTime": event.get("vodTime"),
                    "vodOffsetSeconds": event.get("vodOffsetSeconds"),
                    "start": start,
                    "end": end,
                }
            )

    collected.sort(key=lambda item: item["start"])
    return collected


def merge_nearby(events: list[dict[str, Any]], merge_gap: float) -> list[dict[str, Any]]:
    if not events or merge_gap < 0:
        return events

    merged: list[dict[str, Any]] = []
    current = dict(events[0])
    current["types"] = [current["type"]]
    current["sources"] = [events[0]]

    for event in events[1:]:
        if event["start"] <= current["end"] + merge_gap:
            current["end"] = max(current["end"], event["end"])
            current["types"].append(event["type"])
            current["sources"].append(event)
            # Keep earliest vodTime label for naming.
        else:
            merged.append(current)
            current = dict(event)
            current["types"] = [current["type"]]
            current["sources"] = [event]
    merged.append(current)
    return merged


def cut_clip(source: Path, output: Path, start: float, end: float, reencode: bool) -> None:
    duration = max(0.1, end - start)
    command = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-i",
        str(source),
        "-t",
        f"{duration:.3f}",
    ]
    if reencode:
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
            ]
        )
    else:
        command.extend(["-c", "copy"])
    command.append(str(output))
    run(command)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Cut LoL event clips from an ingested Twitch VOD using lol_events.json."
    )
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        required=True,
        help="Dataset folder, e.g. data/2840148450",
    )
    parser.add_argument(
        "--events",
        type=Path,
        default=None,
        help="Path to lol_events.json (default: <dataset-dir>/lol_events.json)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Clip output folder (default: <dataset-dir>/lol_clips)",
    )
    parser.add_argument(
        "--from-windows",
        type=Path,
        default=None,
        help="Optional lol_events_snapped.json with precomputed windows[]",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="Override path to source video",
    )
    parser.add_argument(
        "--types",
        default="KILL,DEATH,ASSIST",
        help="Comma-separated event types to cut (default: KILL,DEATH,ASSIST)",
    )
    parser.add_argument(
        "--merge-gap",
        type=float,
        default=8.0,
        help="Merge events whose clip windows are within N seconds (default: 8)",
    )
    parser.add_argument(
        "--max-clips",
        type=int,
        default=0,
        help="Optional max clips after merge (0 = no limit)",
    )
    parser.add_argument(
        "--reencode",
        action="store_true",
        help="Re-encode clips (slower, frame-accurate). Default is stream copy.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned clips without cutting",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing lol_clips/manifest",
    )
    parser.add_argument(
        "--timeline-offset",
        type=float,
        default=0.0,
        help=(
            "Seconds to subtract from window start/end when seeking in source "
            "(use when source is a VOD section whose t=0 is not VOD start)."
        ),
    )
    parser.add_argument(
        "--section-start",
        type=float,
        default=None,
        help="Only cut windows overlapping [section-start, section-end] (absolute VOD time)",
    )
    parser.add_argument(
        "--section-end",
        type=float,
        default=None,
        help="Only cut windows overlapping [section-start, section-end] (absolute VOD time)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dataset_dir = args.dataset_dir.resolve()
    events_path = (args.events or (dataset_dir / "lol_events.json")).resolve()
    out_dir = (args.output_dir or (dataset_dir / "lol_clips")).resolve()
    manifest_path = out_dir / "clips.json"

    try:
        types = {t.strip().upper() for t in args.types.split(",") if t.strip()}
        if not types:
            raise ValueError("No event types selected")

        if args.from_windows is not None:
            snapped = load_json(args.from_windows.resolve())
            windows = list(snapped.get("windows") or [])
            events_path = Path(snapped.get("source_events") or args.from_windows)
        else:
            payload = load_json(events_path)
            events = collect_events(payload, types)
            windows = merge_nearby(events, merge_gap=args.merge_gap)

        if args.section_start is not None and args.section_end is not None:
            lo, hi = float(args.section_start), float(args.section_end)
            windows = [
                w
                for w in windows
                if not (float(w["end"]) < lo or float(w["start"]) > hi)
            ]

        if args.max_clips and args.max_clips > 0:
            windows = windows[: args.max_clips]

        timeline_offset = float(args.timeline_offset or 0.0)

        if args.dry_run:
            src_label = str(args.from_windows or events_path)
            print(f"Planned {len(windows)} clip(s) from {src_label}")
            for index, window in enumerate(windows, start=1):
                label = "+".join(dict.fromkeys(window.get("types") or [window["type"]]))
                print(
                    f"{index:02d}  {format_timestamp(window['start'])}-"
                    f"{format_timestamp(window['end'])}  {label}  "
                    f"{window.get('champion')}  {window.get('matchId')}"
                )
            return 0

        source = find_source(dataset_dir, args.source)
        if manifest_path.exists() and not args.force:
            print(
                f"clips.json already exists: {manifest_path}\nPass --force to overwrite.",
                file=sys.stderr,
            )
            return 2

        out_dir.mkdir(parents=True, exist_ok=True)
        assign_game_clip_indices(windows)
        clips: list[dict[str, Any]] = []

        for index, window in enumerate(windows, start=1):
            rel = clip_relpath(window)
            output = out_dir / rel
            output.parent.mkdir(parents=True, exist_ok=True)
            local_start = max(0.0, float(window["start"]) - timeline_offset)
            local_end = max(local_start + 0.1, float(window["end"]) - timeline_offset)
            print(f"[{index}/{len(windows)}] {rel}")
            cut_clip(source, output, local_start, local_end, reencode=args.reencode)
            clips.append(
                {
                    "index": index,
                    "gameIndex": window.get("gameIndex"),
                    "clipIndexInGame": window.get("clipIndexInGame"),
                    "path": str(output),
                    "filename": rel.name,
                    "relativePath": rel.as_posix(),
                    "start": window["start"],
                    "end": window["end"],
                    "localStart": round(local_start, 3),
                    "localEnd": round(local_end, 3),
                    "timelineOffset": timeline_offset,
                    "duration": round(window["end"] - window["start"], 3),
                    "types": window.get("types") or [window["type"]],
                    "matchId": window.get("matchId"),
                    "champion": window.get("champion"),
                    "opponentChampion": window.get("opponentChampion"),
                    "win": window.get("win"),
                    "queueId": window.get("queueId"),
                    "teamPosition": window.get("teamPosition"),
                    "vodTime": window.get("vodTime"),
                    "gameTime": window.get("gameTime"),
                    "transcript": window.get("transcript"),
                    "startReason": window.get("startReason"),
                    "endReason": window.get("endReason"),
                    "source_event_count": len(window.get("sources") or [window]),
                }
            )

        manifest = {
            "schema_version": 1,
            "dataset_id": dataset_dir.name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_path": str(source),
            "events_path": str(events_path),
            "from_windows": str(args.from_windows) if args.from_windows else None,
            "output_dir": str(out_dir),
            "types": sorted(types),
            "merge_gap": args.merge_gap,
            "reencode": args.reencode,
            "timeline_offset": timeline_offset,
            "section_start": args.section_start,
            "section_end": args.section_end,
            "clip_count": len(clips),
            "clips": clips,
        }
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({"status": "ok", "clip_count": len(clips), "dir": str(out_dir)}, indent=2))
        return 0

    except Exception as exc:
        print(f"Cut failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
