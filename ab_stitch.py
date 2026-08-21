#!/usr/bin/env python3
"""A/B recap weaves from existing lol_clips/ — selection only, same stitch settings.

Variants (blind-labeled A–D per game):
  current       top 5 + reserved GAME_END
  competitive   top 5 by score (closer competes)
  top8          top 8 by score
  duration      score >= 3.5 until ~150s

  python ab_stitch.py --dataset-dir data/aug17_2026_2849217240 --games 2,5,9
  python ab_stitch.py --vod-id 2849217240 --restore --games 1,2,5,7,9,10,11,12,14,15
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from env_loader import load_dotenv

ROOT = Path(__file__).resolve().parent
VARIANTS = (
    ("current", "top 5 + reserved closer"),
    ("competitive", "top 5, closer competes"),
    ("top8", "top 8 by score"),
    ("duration", "score floor 3.5 / max 150s"),
)
LETTERS = ("A", "B", "C", "D")
DEFAULT_GAMES = (1, 2, 5, 7, 9, 10, 11, 12, 14, 15)


def parse_games(raw: str) -> list[int]:
    text = (raw or "").strip()
    if not text:
        return list(DEFAULT_GAMES)
    out: list[int] = []
    for part in text.split(","):
        part = part.strip().lstrip("gG")
        if part:
            out.append(int(part))
    return out


def folder_index(name: str) -> int | None:
    head = name.split("_", 1)[0]
    if head[:1].lower() != "g":
        return None
    try:
        return int(head[1:])
    except ValueError:
        return None


def restore_needed(
    vod_id: str,
    dataset_dir: Path,
    folders: list[str],
) -> None:
    import storage_gcs as gcs

    vid = vod_id.strip().lstrip("v")
    day = gcs.resolve_day_key(vid, dataset_dir)
    base = gcs.vod_prefix(vid, day_key=day)
    clips_dir = dataset_dir / "lol_clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    for name in (
        "clips.json",
        "clip_scores.json",
        "top_picks.json",
    ):
        remote = f"{base}/lol_clips/{name}"
        dest = clips_dir / name
        if dest.is_file():
            continue
        if gcs.blob_exists(remote):
            print(f"[restore] {remote}", flush=True)
            gcs.download_file(remote, dest)

    for name in (
        "transcript.json",
        "lol_events_snapped.json",
        "lol_events.json",
        "metadata.json",
    ):
        dest = dataset_dir / name
        if dest.is_file():
            continue
        remote = f"{base}/{name}"
        if gcs.blob_exists(remote):
            print(f"[restore] {remote}", flush=True)
            gcs.download_file(remote, dest)

    for folder in folders:
        local = clips_dir / folder
        mp4s = list(local.glob("c*.mp4")) if local.is_dir() else []
        if mp4s:
            print(f"[restore] have {folder} ({len(mp4s)} mp4)", flush=True)
            continue
        remote = f"{base}/lol_clips/{folder}/"
        print(f"[restore] {remote}", flush=True)
        files = gcs.download_prefix(remote, local, skip_existing=True)
        print(f"  → {len(files)} file(s)", flush=True)


def pick_rows(picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "file": c.get("relativePath"),
            "score": c.get("score"),
            "types": c.get("types"),
            "why": c.get("why"),
            "duration": c.get("duration"),
            "closerScore": c.get("closerScore"),
        }
        for c in picks
    ]


def main() -> int:
    load_dotenv()
    p = argparse.ArgumentParser(description="Blind A/B recap weaves from lol_clips/")
    p.add_argument("--dataset-dir", type=Path, default=None)
    p.add_argument("--vod-id", default="2849217240")
    p.add_argument("--games", default=",".join(str(g) for g in DEFAULT_GAMES))
    p.add_argument("--restore", action="store_true", help="Pull missing clip folders from GCS")
    p.add_argument("--audio", action="store_true", help="Also write audio_features.json (cheap)")
    p.add_argument("--force", action="store_true")
    p.add_argument("--min-score", type=float, default=3.5)
    p.add_argument("--max-duration", type=float, default=150.0)
    args = p.parse_args()

    vod_id = str(args.vod_id).strip().lstrip("v")
    dataset_dir = (
        args.dataset_dir.resolve()
        if args.dataset_dir
        else (ROOT / "data" / f"aug17_2026_{vod_id}").resolve()
    )
    clips_dir = dataset_dir / "lol_clips"
    want = set(parse_games(args.games))

    if args.restore:
        import storage_gcs as gcs

        folders: list[str] = []
        if clips_dir.is_dir():
            folders = [
                p.name
                for p in sorted(clips_dir.iterdir())
                if p.is_dir() and folder_index(p.name) in want
            ]
        if len(folders) < len(want):
            day = gcs.resolve_day_key(vod_id, dataset_dir)
            base = gcs.vod_prefix(vod_id, day_key=day)
            manifest_remote = f"{base}/lol_clips/clips.json"
            manifest_local = clips_dir / "clips.json"
            if gcs.blob_exists(manifest_remote):
                gcs.download_file(manifest_remote, manifest_local)
            if manifest_local.is_file():
                payload = json.loads(manifest_local.read_text(encoding="utf-8"))
                names = {
                    str(item.get("relativePath") or "").split("/", 1)[0]
                    for item in payload.get("clips") or []
                    if str(item.get("relativePath") or "").count("/")
                }
                folders = sorted(n for n in names if folder_index(n) in want)
        restore_needed(vod_id, dataset_dir, folders)

    if not clips_dir.is_dir():
        print(f"missing {clips_dir} (pass --restore)", file=sys.stderr)
        return 1

    from score_clips import pick_by_selector, rank_dataset
    from stitch_game_clips import discover_games, probe_video, stitch_one

    games = [
        (folder, clips)
        for folder, clips in discover_games(clips_dir)
        if folder_index(folder) in want
    ]
    if not games:
        print(f"no local games matching {sorted(want)} under {clips_dir}", file=sys.stderr)
        return 1

    scored = [
        c
        for c in rank_dataset(dataset_dir, clips_dir)
        if Path(str(c.get("path") or "")).is_file()
        and folder_index(str(c.get("gameFolder") or "")) in want
    ]
    print(f"[ab] {len(games)} game(s), {len(scored)} scored clips", flush=True)

    ab_root = dataset_dir / "ab"
    ab_root.mkdir(parents=True, exist_ok=True)
    index: dict[str, Any] = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "vodId": vod_id,
        "dataset": str(dataset_dir),
        "note": "Filenames A–D are shuffled per game. Mapping is in each folder's key.json — don't peek until after rating.",
        "variants": [{"id": vid, "label": label} for vid, label in VARIANTS],
        "games": [],
    }

    for folder, clips in games:
        dest = ab_root / folder
        dest.mkdir(parents=True, exist_ok=True)
        rng = random.Random(folder)
        order = list(VARIANTS)
        rng.shuffle(order)
        letter_of = {vid: letter for letter, (vid, _) in zip(LETTERS, order)}
        key = {
            "gameFolder": folder,
            "blind": {letter: vid for vid, letter in letter_of.items()},
            "labels": {vid: label for vid, label in VARIANTS},
        }
        (dest / "key.json").write_text(
            json.dumps(key, indent=2) + "\n", encoding="utf-8"
        )

        group = [c for c in scored if c.get("gameFolder") == folder]
        probe = probe_video(clips[0])
        game_row: dict[str, Any] = {
            "folder": folder,
            "dir": str(dest),
            "letters": {},
        }
        print(f"\n[ab] {folder}", flush=True)
        for selector, label in VARIANTS:
            picks = pick_by_selector(
                group,
                selector,
                per_game=True,
                min_score=float(args.min_score),
                max_duration=float(args.max_duration),
            )
            letter = letter_of[selector]
            out_mp4 = dest / f"{letter}.mp4"
            names = {Path(c["relativePath"]).name for c in picks}
            kept = [p for p in clips if p.name in names]
            dur = round(sum(float(c.get("duration") or 0.0) for c in picks), 2)
            print(
                f"  {letter}={selector}  n={len(kept)}  ~{dur:.0f}s  "
                f"{[c.get('filename') for c in picks]}",
                flush=True,
            )
            if out_mp4.exists() and not args.force:
                print(f"  [skip] {out_mp4.name}", flush=True)
            elif len(kept) < 2:
                print(f"  [skip] {selector}: only {len(kept)} clip(s)", flush=True)
            else:
                stitch_one(
                    kept,
                    out_mp4,
                    reencode=False,
                    width=int(probe["width"]),
                    height=int(probe["height"]),
                    fps=float(probe["fps"]),
                    detect_freeze=True,
                )
            game_row["letters"][letter] = {
                "selector": selector,
                "label": label,
                "file": out_mp4.name,
                "clipCount": len(kept),
                "seconds": dur,
                "picks": pick_rows(picks),
            }
        (dest / "picks.json").write_text(
            json.dumps(game_row, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        index["games"].append(
            {
                "folder": folder,
                "dir": f"ab/{folder}",
                "files": [f"{letter}.mp4" for letter in LETTERS],
            }
        )

    (ab_root / "index.json").write_text(
        json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"\n[ab] wrote {ab_root / 'index.json'}", flush=True)
    print("Rate A–D without opening key.json. Unblind after.", flush=True)

    if args.audio:
        from audio_loudness import analyze_clips_dir

        payload = analyze_clips_dir(clips_dir)
        out = clips_dir / "audio_features.json"
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"[audio] {out} ({payload['clip_count']} clips)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
