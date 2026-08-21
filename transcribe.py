#!/usr/bin/env python3
"""Transcribe an ingested VOD dataset into timed transcript segments."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load_ingest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"ingest.json not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


from dataset_paths import find_dataset_dir


def resolve_dataset_dir(args: argparse.Namespace) -> Path:
    if args.dataset_dir is not None:
        return args.dataset_dir.resolve()
    if args.dataset_id is not None:
        found = find_dataset_dir(args.output_root, args.dataset_id)
        if found:
            return found.resolve()
        return (args.output_root / args.dataset_id).resolve()
    raise ValueError("Provide --dataset-id or --dataset-dir.")


def transcribe_audio(
    audio_path: Path,
    model_name: str,
    language: str | None,
    device: str,
    compute_type: str,
) -> list[dict[str, Any]]:
    from faster_whisper import WhisperModel

    model = WhisperModel(model_name, device=device, compute_type=compute_type)
    segments_iter, _info = model.transcribe(
        str(audio_path),
        language=language,
        vad_filter=True,
        word_timestamps=False,
    )

    segments: list[dict[str, Any]] = []
    for segment in segments_iter:
        text = segment.text.strip()
        if not text:
            continue
        segments.append({
            "start": round(float(segment.start), 3),
            "end": round(float(segment.end), 3),
            "text": text,
        })
    return segments


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transcribe an ingested VOD using faster-whisper."
    )
    parser.add_argument("--dataset-id", help="Dataset ID under the output root")
    parser.add_argument("--dataset-dir", type=Path, help="Path to dataset folder")
    parser.add_argument("--output-root", type=Path, default=Path("data"))
    parser.add_argument("--model", default="small.en")
    parser.add_argument("--language", default="en")
    parser.add_argument("--device", default="cpu", choices=["cpu", "cuda", "auto"])
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        dataset_dir = resolve_dataset_dir(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    ingest_path = dataset_dir / "ingest.json"
    transcript_path = dataset_dir / "transcript.json"

    try:
        if transcript_path.exists() and not args.force:
            print(f"transcript.json already exists: {transcript_path}\nPass --force to overwrite.", file=sys.stderr)
            return 2

        ingest = load_ingest(ingest_path)
        audio_path = Path(ingest["audio_path"])
        if not audio_path.is_file():
            raise FileNotFoundError(f"Audio file missing: {audio_path}")

        language = None if args.language == "auto" else args.language
        segments = transcribe_audio(
            audio_path=audio_path,
            model_name=args.model,
            language=language,
            device=args.device,
            compute_type=args.compute_type,
        )

        payload = {
            "schema_version": 1,
            "dataset_id": ingest.get("dataset_id"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_audio": str(audio_path),
            "model": args.model,
            "language": args.language,
            "segment_count": len(segments),
            "segments": segments,
        }
        transcript_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

        print(json.dumps({
            "status": "ok",
            "dataset_id": ingest.get("dataset_id"),
            "transcript": str(transcript_path),
            "segment_count": len(segments),
            "model": args.model,
        }, indent=2))
        return 0
    except Exception as exc:
        print(f"Transcription failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
