#!/usr/bin/env python3
"""Cut candidate clips from top-scoring timeline windows."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


def merge_overlaps(windows, pad_before, pad_after, duration, merge_gap):
    expanded = []
    for window in windows:
        start = max(0.0, float(window["start"]) - pad_before)
        end = min(duration, float(window["end"]) + pad_after)
        expanded.append({
            "start": start, "end": end,
            "score": float(window.get("score", 0.0)),
            "rank": int(window.get("rank", 0)),
            "text": window.get("text", ""),
            "keyword_hits": window.get("keyword_hits", []),
            "source_windows": [window],
        })
    if not expanded:
        return []
    expanded.sort(key=lambda item: item["start"])
    merged = [expanded[0]]
    for item in expanded[1:]:
        current = merged[-1]
        if item["start"] <= current["end"] + merge_gap:
            current["end"] = max(current["end"], item["end"])
            current["score"] = max(current["score"], item["score"])
            current["rank"] = min(current["rank"], item["rank"]) if current["rank"] else item["rank"]
            if item.get("text") and item["text"] not in current["text"]:
                current["text"] = f"{current['text']} | {item['text']}" if current["text"] else item["text"]
            current["keyword_hits"] = list(dict.fromkeys([*current.get("keyword_hits", []), *item.get("keyword_hits", [])]))
            current["source_windows"].extend(item["source_windows"])
        else:
            merged.append(item)
    merged.sort(key=lambda item: item["score"], reverse=True)
    return merged


def format_timestamp(seconds: float) -> str:
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}h{minutes:02d}m{secs:02d}s"


def cut_clip(source: Path, output: Path, start: float, end: float, reencode: bool) -> None:
    duration = max(0.1, end - start)
    command = ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{duration:.3f}"]
    if reencode:
        command.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"])
    else:
        command.extend(["-c", "copy"])
    command.append(str(output))
    run(command)


def parse_args():
    p = argparse.ArgumentParser(description="Extract candidate clips from scores.json")
    p.add_argument("--dataset-id")
    p.add_argument("--dataset-dir", type=Path)
    p.add_argument("--output-root", type=Path, default=Path("data"))
    p.add_argument("--top-k", type=int, default=15)
    p.add_argument("--pad-before", type=float, default=5.0)
    p.add_argument("--pad-after", type=float, default=5.0)
    p.add_argument("--merge-gap", type=float, default=3.0)
    p.add_argument("--max-clips", type=int, default=10)
    p.add_argument("--reencode", action="store_true")
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    try:
        dataset_dir = resolve_dataset_dir(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    candidates_dir = dataset_dir / "candidates"
    manifest_path = candidates_dir / "candidates.json"
    try:
        if manifest_path.exists() and not args.force:
            print(f"candidates.json already exists: {manifest_path}\nPass --force to overwrite.", file=sys.stderr)
            return 2

        ingest = load_json(dataset_dir / "ingest.json")
        scores = load_json(dataset_dir / "scores.json")
        source = Path(ingest["source_path"])
        if not source.is_file():
            raise FileNotFoundError(f"Source video missing: {source}")

        duration = float(ingest.get("duration_seconds") or scores.get("duration_seconds") or 0.0)
        selected = sorted(scores.get("top_windows") or [], key=lambda w: int(w.get("rank", 10**9)))[:args.top_k]
        merged = merge_overlaps(selected, args.pad_before, args.pad_after,
                                duration if duration > 0 else 10**9, args.merge_gap)[:args.max_clips]

        candidates_dir.mkdir(parents=True, exist_ok=True)
        if args.force:
            for old in candidates_dir.glob("clip_*.mp4"):
                old.unlink()

        clips = []
        for index, item in enumerate(merged, start=1):
            start, end = round(float(item["start"]), 3), round(float(item["end"]), 3)
            filename = f"clip_{index:02d}_{format_timestamp(start)}.mp4"
            output = candidates_dir / filename
            cut_clip(source, output, start, end, reencode=args.reencode)
            clips.append({
                "index": index, "path": str(output), "filename": filename,
                "start": start, "end": end, "duration": round(end - start, 3),
                "score": round(float(item["score"]), 3), "best_rank": int(item["rank"]),
                "keyword_hits": item.get("keyword_hits", []), "text": item.get("text", ""),
                "source_window_count": len(item.get("source_windows", [])),
            })

        manifest = {
            "schema_version": 1,
            "dataset_id": ingest.get("dataset_id"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_path": str(source),
            "pad_before": args.pad_before, "pad_after": args.pad_after,
            "merge_gap": args.merge_gap, "top_k": args.top_k,
            "max_clips": args.max_clips, "reencode": args.reencode,
            "clip_count": len(clips), "clips": clips,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({
            "status": "ok",
            "dataset_id": ingest.get("dataset_id"),
            "candidates_dir": str(candidates_dir),
            "manifest": str(manifest_path),
            "clip_count": len(clips),
            "clips": [{
                "index": c["index"], "filename": c["filename"],
                "start": c["start"], "end": c["end"],
                "duration": c["duration"], "score": c["score"],
                "text": (c["text"][:100] + "…") if len(c["text"]) > 100 else c["text"],
            } for c in clips],
        }, indent=2))
        return 0
    except Exception as exc:
        print(f"Extraction failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
