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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dataset_dir = args.dataset_dir.resolve()
    events_path = (args.events or (dataset_dir / "lol_events.json")).resolve()
    out_dir = dataset_dir / "lol_clips"
    manifest_path = out_dir / "clips.json"

    try:
        payload = load_json(events_path)
        types = {t.strip().upper() for t in args.types.split(",") if t.strip()}
        if not types:
            raise ValueError("No event types selected")

        events = collect_events(payload, types)
        windows = merge_nearby(events, merge_gap=args.merge_gap)
        if args.max_clips and args.max_clips > 0:
            windows = windows[: args.max_clips]

        if args.dry_run:
            print(f"Planned {len(windows)} clip(s) from {events_path}")
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
        clips: list[dict[str, Any]] = []

        for index, window in enumerate(windows, start=1):
            label = "+".join(dict.fromkeys(window.get("types") or [window["type"]]))
            filename = (
                f"clip_{index:02d}_{label.lower()}_{format_timestamp(window['start'])}.mp4"
            )
            output = out_dir / filename
            print(f"[{index}/{len(windows)}] {filename}")
            cut_clip(source, output, window["start"], window["end"], reencode=args.reencode)
            clips.append(
                {
                    "index": index,
                    "path": str(output),
                    "filename": filename,
                    "start": window["start"],
                    "end": window["end"],
                    "duration": round(window["end"] - window["start"], 3),
                    "types": window.get("types") or [window["type"]],
                    "matchId": window.get("matchId"),
                    "champion": window.get("champion"),
                    "vodTime": window.get("vodTime"),
                    "gameTime": window.get("gameTime"),
                    "source_event_count": len(window.get("sources") or [window]),
                }
            )

        manifest = {
            "schema_version": 1,
            "dataset_id": dataset_dir.name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_path": str(source),
            "events_path": str(events_path),
            "types": sorted(types),
            "merge_gap": args.merge_gap,
            "reencode": args.reencode,
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
