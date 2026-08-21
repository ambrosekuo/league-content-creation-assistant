#!/usr/bin/env python3
"""Score overlapping timeline windows for clip-worthy moments."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

KEYWORD_WEIGHTS: dict[str, float] = {
    "oh my god": 3.0, "oh my": 2.0, "what the": 2.0, "no way": 2.5,
    "holy": 2.0, "insane": 2.5, "crazy": 1.5, "clip that": 4.0,
    "clip this": 4.0, "let's go": 2.0, "lets go": 2.0, "come on": 1.5,
    "get out": 2.0, "outplayed": 2.5, "one shot": 2.0, "oneshot": 2.0,
    "triple": 3.0, "quadra": 4.0, "penta": 5.0, "pentakill": 5.0,
    "ace": 3.0, "first blood": 2.5, "shut down": 2.0, "shutdown": 2.0,
    "flashed": 2.0, "flash": 1.2, "ult": 1.0, "engage": 1.5,
    "all in": 1.5, "gank": 1.5, "dive": 1.5, "baron": 2.0,
    "dragon": 1.5, "elder": 2.5, "leblanc": 1.0, "diff": 1.5,
    "int": 1.2, "bait": 1.5, "caught": 1.5, "cooking": 1.5,
    "send it": 2.0, "wow": 1.5, "what": 0.6,
}


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


def overlapping_text(segments, start, end):
    return [
        s for s in segments
        if not (float(s["end"]) < start or float(s["start"]) > end)
    ]


def keyword_score(text: str) -> tuple[float, list[str]]:
    lowered = text.lower()
    score, hits = 0.0, []
    for phrase, weight in KEYWORD_WEIGHTS.items():
        if phrase in lowered:
            score += weight
            hits.append(phrase)
    return score, hits


def exclamation_bonus(text: str) -> float:
    marks = text.count("!")
    caps = sum(1 for w in re.findall(r"[A-Za-z']+", text) if w.isupper() and len(w) > 2)
    return min(2.0, 0.25 * marks + 0.15 * caps)


def speech_density_score(segments, window_seconds: float) -> float:
    if not segments or window_seconds <= 0:
        return 0.0
    spoken = sum(max(0.0, float(s["end"]) - float(s["start"])) for s in segments)
    return min(1.0, spoken / window_seconds) * 1.5


def read_pcm16_mono(path: Path) -> tuple[list[int], int]:
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError("Expected 16-bit mono PCM WAV")
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    return list(memoryview(frames).cast("h")), rate


def rms_for_window(samples, rate, start, end) -> float:
    a, b = max(0, int(start * rate)), min(len(samples), int(end * rate))
    chunk = samples[a:b]
    if not chunk:
        return 0.0
    return math.sqrt(sum(x * x for x in chunk) / len(chunk))


def energy_scores(rms_values):
    if not rms_values:
        return []
    peak = max(rms_values) or 1.0
    mean = sum(rms_values) / len(rms_values)
    return [((r / peak) * 1.5) + (max(0.0, (r - mean) / peak) * 2.5) for r in rms_values]


def build_windows(duration, window_seconds, hop_seconds):
    windows, start = [], 0.0
    while start < duration:
        end = min(duration, start + window_seconds)
        windows.append((start, end))
        if end >= duration:
            break
        start += hop_seconds
    return windows


def score_dataset(ingest, transcript, window_seconds, hop_seconds, top_k, use_energy):
    duration = float(ingest.get("duration_seconds") or 0.0)
    segments = transcript.get("segments") or []
    if duration <= 0 and segments:
        duration = max(float(s["end"]) for s in segments)

    windows = build_windows(duration, window_seconds, hop_seconds)
    if use_energy:
        samples, rate = read_pcm16_mono(Path(ingest["audio_path"]))
        rms_values = [rms_for_window(samples, rate, s, e) for s, e in windows]
        energy = energy_scores(rms_values)
    else:
        energy = [0.0] * len(windows)

    scored = []
    for index, ((start, end), e_score) in enumerate(zip(windows, energy)):
        hits = overlapping_text(segments, start, end)
        text = " ".join(str(s.get("text", "")).strip() for s in hits).strip()
        kw_score, kw_hits = keyword_score(text)
        density = speech_density_score(hits, end - start)
        excl = exclamation_bonus(text)
        total = kw_score + density + excl + e_score
        scored.append({
            "rank": 0,
            "start": round(start, 3),
            "end": round(end, 3),
            "score": round(total, 3),
            "components": {
                "keywords": round(kw_score, 3),
                "speech_density": round(density, 3),
                "exclamation": round(excl, 3),
                "energy": round(e_score, 3),
            },
            "keyword_hits": kw_hits,
            "text": text,
            "segment_count": len(hits),
            "window_index": index,
        })

    scored.sort(key=lambda item: item["score"], reverse=True)
    for rank, item in enumerate(scored, start=1):
        item["rank"] = rank

    return {
        "schema_version": 1,
        "dataset_id": ingest.get("dataset_id"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "window_seconds": window_seconds,
        "hop_seconds": hop_seconds,
        "duration_seconds": duration,
        "window_count": len(scored),
        "top_k": top_k,
        "use_energy": use_energy,
        "top_windows": scored[:top_k],
        "windows": scored,
    }


def parse_args():
    p = argparse.ArgumentParser(description="Score timeline windows for clip candidates.")
    p.add_argument("--dataset-id")
    p.add_argument("--dataset-dir", type=Path)
    p.add_argument("--output-root", type=Path, default=Path("data"))
    p.add_argument("--window-seconds", type=float, default=10.0)
    p.add_argument("--hop-seconds", type=float, default=5.0)
    p.add_argument("--top-k", type=int, default=40)
    p.add_argument("--no-energy", action="store_true")
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    try:
        dataset_dir = resolve_dataset_dir(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    output_path = dataset_dir / "scores.json"
    try:
        if output_path.exists() and not args.force:
            print(f"scores.json already exists: {output_path}\nPass --force to overwrite.", file=sys.stderr)
            return 2

        payload = score_dataset(
            ingest=load_json(dataset_dir / "ingest.json"),
            transcript=load_json(dataset_dir / "transcript.json"),
            window_seconds=args.window_seconds,
            hop_seconds=args.hop_seconds,
            top_k=args.top_k,
            use_energy=not args.no_energy,
        )
        output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        preview = [{
            "rank": w["rank"], "start": w["start"], "end": w["end"], "score": w["score"],
            "keyword_hits": w["keyword_hits"],
            "text": (w["text"][:120] + "…") if len(w["text"]) > 120 else w["text"],
        } for w in payload["top_windows"][:10]]
        print(json.dumps({
            "status": "ok",
            "dataset_id": payload["dataset_id"],
            "scores": str(output_path),
            "window_count": payload["window_count"],
            "top_preview": preview,
        }, indent=2))
        return 0
    except Exception as exc:
        print(f"Scoring failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
