#!/usr/bin/env python3
"""
Transcribe short audio windows around LoL events (not the full VOD).

Writes transcript.json with absolute VOD timestamps so snap_clips_to_transcript.py
can avoid mid-sentence cuts without a multi-hour full-stream whisper.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from asr import (
    DEFAULT_WHISPER_MODEL,
    build_prompt,
    default_model_for,
    matchup_from_name,
    resolve_engine,
    transcribe_words,
    words_to_segments,
)
from env_loader import load_dotenv


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Missing file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def find_source(dataset_dir: Path) -> Path:
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
            and "source" in p.name.lower()
            and ".part" not in p.name
        ],
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No source video in {dataset_dir}")
    return candidates[0]


def collect_anchors(events_payload: dict[str, Any], types: set[str]) -> list[float]:
    anchors: list[float] = []
    for match in events_payload.get("matches") or []:
        for event in match.get("events") or []:
            if event.get("type") not in types:
                continue
            off = event.get("vodOffsetSeconds")
            if off is None:
                continue
            anchors.append(float(off))
    anchors.sort()
    return anchors


def merge_anchors(anchors: list[float], merge_gap: float) -> list[tuple[float, float]]:
    """Return (window_start, window_end) pads merged when close."""
    if not anchors:
        return []
    # Caller expands each anchor; here we only cluster anchor times.
    clusters: list[list[float]] = [[anchors[0]]]
    for t in anchors[1:]:
        if t - clusters[-1][-1] <= merge_gap:
            clusters[-1].append(t)
        else:
            clusters.append([t])
    return [(c[0], c[-1]) for c in clusters]


def extract_wav(
    source: Path,
    out_wav: Path,
    *,
    start: float,
    duration: float,
) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(out_wav),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-500:]}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Whisper only around LoL event windows; write transcript.json"
    )
    p.add_argument("--dataset-dir", type=Path, required=True)
    p.add_argument("--events", type=Path, default=None)
    p.add_argument("--types", default="KILL,DEATH,ASSIST")
    p.add_argument("--pre-roll", type=float, default=25.0)
    p.add_argument("--post-roll", type=float, default=20.0)
    p.add_argument(
        "--merge-gap",
        type=float,
        default=45.0,
        help="Merge nearby event anchors into one whisper window",
    )
    p.add_argument(
        "--asr",
        dest="asr_engine",
        default=None,
        choices=("whisper", "openai"),
        help="ASR engine. Default: $TRANSCRIBE_ASR or local whisper",
    )
    p.add_argument(
        "--model",
        default=None,
        help="ASR model. Default: small.en (whisper) or gpt-4o-mini-transcribe (openai)",
    )
    p.add_argument(
        "--prompt",
        dest="asr_prompt",
        default="",
        help="Extra ASR prompt (League terms are added automatically)",
    )
    p.add_argument(
        "--align",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="When --asr openai, time words with local Whisper (default: on)",
    )
    p.add_argument(
        "--align-model",
        default=DEFAULT_WHISPER_MODEL,
        help="Local Whisper model used only to time OpenAI words",
    )
    p.add_argument("--language", default="en")
    p.add_argument("--device", default="cpu", choices=["cpu", "cuda", "auto"])
    p.add_argument("--compute-type", default="int8")
    p.add_argument("--force", action="store_true")
    p.add_argument(
        "--section-start",
        type=float,
        default=None,
        help="Only whisper anchors inside this absolute VOD window",
    )
    p.add_argument(
        "--section-end",
        type=float,
        default=None,
        help="Only whisper anchors inside this absolute VOD window",
    )
    p.add_argument(
        "--timeline-offset",
        type=float,
        default=0.0,
        help="Source file starts at this absolute VOD second (section download)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    dataset_dir = args.dataset_dir.resolve()
    events_path = (args.events or dataset_dir / "lol_events.json").resolve()
    transcript_path = dataset_dir / "transcript.json"
    asr_engine = resolve_engine(
        args.asr_engine or os.environ.get("TRANSCRIBE_ASR"),
        default="whisper",
    )
    model_name = str(args.model or default_model_for(asr_engine))

    try:
        if transcript_path.is_file() and not args.force:
            print(
                f"transcript.json already exists: {transcript_path} (pass --force)",
                file=sys.stderr,
            )
            return 0

        events = load_json(events_path)
        types = {t.strip().upper() for t in args.types.split(",") if t.strip()}
        anchors = collect_anchors(events, types)
        if args.section_start is not None and args.section_end is not None:
            lo, hi = float(args.section_start), float(args.section_end)
            anchors = [a for a in anchors if lo <= a <= hi]
        clusters = merge_anchors(anchors, args.merge_gap)
        if not clusters:
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "transcript": str(transcript_path),
                        "window_count": 0,
                        "segment_count": 0,
                        "note": "no anchors in section",
                    },
                    indent=2,
                )
            )
            transcript_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "dataset_id": dataset_dir.name,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "mode": "event_windows",
                        "segment_count": 0,
                        "segments": [],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            return 0

        source = find_source(dataset_dir)
        language = None if args.language == "auto" else args.language
        timeline_offset = float(args.timeline_offset or 0.0)
        segments: list[dict[str, Any]] = []
        matchup = matchup_from_name(dataset_dir.name)
        prompt = build_prompt(
            champion=matchup.get("champion") or "",
            opponent=matchup.get("opponent") or "",
            extra=str(args.asr_prompt or ""),
        )

        print(
            f"[transcribe-windows] {len(anchors)} anchors → {len(clusters)} windows "
            f"engine={asr_engine} model={model_name} offset={timeline_offset}",
            flush=True,
        )

        with tempfile.TemporaryDirectory(prefix="event-asr-") as tmp:
            tmp_dir = Path(tmp)

            for index, (a0, a1) in enumerate(clusters, start=1):
                abs_start = max(0.0, a0 - args.pre_roll)
                abs_end = a1 + args.post_roll
                local_start = max(0.0, abs_start - timeline_offset)
                duration = max(1.0, abs_end - abs_start)
                wav = tmp_dir / f"w{index:03d}.wav"
                print(
                    f"  [{index}/{len(clusters)}] abs {abs_start:.1f}–{abs_end:.1f}s "
                    f"(local {local_start:.1f}, {duration:.0f}s)",
                    flush=True,
                )
                extract_wav(source, wav, start=local_start, duration=duration)
                words, _detected, _meta = transcribe_words(
                    wav,
                    engine=asr_engine,
                    model_name=model_name,
                    language=language,
                    device=args.device,
                    compute_type=args.compute_type,
                    prompt=prompt,
                    align=bool(args.align),
                    align_model=str(args.align_model or DEFAULT_WHISPER_MODEL),
                    filter_low_confidence=False,
                )
                window_segments = words_to_segments(words)
                if not window_segments and _meta.get("raw_text"):
                    window_segments = [
                        {
                            "start": 0.0,
                            "end": duration,
                            "text": str(_meta["raw_text"]),
                        }
                    ]
                for segment in window_segments:
                    text = str(segment.get("text") or "").strip()
                    if not text:
                        continue
                    segments.append(
                        {
                            "start": round(abs_start + float(segment["start"]), 3),
                            "end": round(abs_start + float(segment["end"]), 3),
                            "text": text,
                        }
                    )

        segments.sort(key=lambda s: float(s["start"]))
        payload = {
            "schema_version": 1,
            "dataset_id": dataset_dir.name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_audio": str(source),
            "engine": asr_engine,
            "model": model_name,
            "language": args.language,
            "mode": "event_windows",
            "window_count": len(clusters),
            "segment_count": len(segments),
            "segments": segments,
        }
        transcript_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "transcript": str(transcript_path),
                    "window_count": len(clusters),
                    "segment_count": len(segments),
                },
                indent=2,
            )
        )
        return 0
    except Exception as exc:
        print(f"Event-window transcription failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
