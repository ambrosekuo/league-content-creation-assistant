#!/usr/bin/env python3
"""Mix a chosen pool track onto decorated portraits.

Decorate stays mute (combos, captions, wrap). This is the last export step.

  python mix_portrait_music.py --dataset-dir data/{day}_{vod} --from-picks --track a-game
  python mix_portrait_music.py --input ..._portrait_decorated.mp4 --track dance-zero-nc
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from dataset_paths import find_dataset_dir, game_only_matches
from env_loader import load_dotenv
from music_pool import mix_pool_bed, resolve_pool_track, track_file

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

PORTRAIT_DIRS = (
    "lol_compilations_picks_portrait",
    "lol_compilations_portrait",
)


def run_print(msg: str) -> None:
    print(msg, flush=True)


def is_decorated_portrait(path: Path) -> bool:
    stem = path.stem.lower()
    if path.suffix.lower() != ".mp4" or not path.is_file():
        return False
    if not stem.endswith("_portrait_decorated"):
        return False
    return "_music" not in stem


def music_output_for(decorated: Path) -> Path:
    stem = decorated.stem
    if stem.endswith("_portrait_decorated"):
        base = stem[: -len("_portrait_decorated")]
        name = f"{base}_portrait_music.mp4"
    else:
        name = f"{stem}_music.mp4"
    return decorated.parent / "post" / name


def legacy_music_output_for(decorated: Path) -> Path:
    stem = decorated.stem
    if stem.endswith("_portrait_decorated"):
        base = stem[: -len("_portrait_decorated")]
        return decorated.with_name(f"{base}_portrait_music.mp4")
    return decorated.with_name(f"{stem}_music.mp4")


def sidecar_for(dest: Path) -> Path:
    return dest.with_suffix(".json")


def discover_decorated(dataset_dir: Path, *, only: str, picks_only: bool) -> list[Path]:
    found: list[Path] = []
    dirs = PORTRAIT_DIRS[:1] if picks_only else PORTRAIT_DIRS
    for folder_name in dirs:
        folder = dataset_dir / folder_name
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.mp4")):
            if not is_decorated_portrait(path):
                continue
            if only and not game_only_matches(path.name, only):
                continue
            found.append(path)
    seen: set[Path] = set()
    out: list[Path] = []
    for path in found:
        key = path.resolve()
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def mix_one(
    source: Path,
    *,
    track_query: str,
    output: Path | None,
    force: bool,
    music_db: float,
) -> dict[str, Any]:
    source = source.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    dest = (output or music_output_for(source)).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not output:
        legacy = legacy_music_output_for(source)
        if dest != legacy.resolve() and legacy.is_file() and not dest.is_file():
            legacy.replace(dest)
            old_side = sidecar_for(legacy)
            if old_side.is_file():
                old_side.replace(sidecar_for(dest))
    if dest.is_file() and not force:
        run_print(f"[skip] exists {dest.name} (pass --force)")
        return {"source": str(source), "output": str(dest), "skipped": True}

    chosen = resolve_pool_track(track_query)
    audio = track_file(chosen)
    if not audio.is_file():
        raise FileNotFoundError(f"missing pool audio: {audio}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    # Avoid leading-dot temps: ffmpeg +faststart fails to re-open them on macOS.
    tmp = dest.with_name(f"{dest.stem}._mix.tmp.mp4")
    try:
        mix_pool_bed(source, chosen, tmp, music_db=float(music_db))
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)

    report = {
        "track": chosen.get("id"),
        "name": chosen.get("name"),
        "db": float(music_db),
        "source": source.name,
        "output": dest.name,
    }
    sidecar_for(dest).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    run_print(f"[music] {chosen.get('id')} → {dest.name}")
    return {
        "source": str(source),
        "output": str(dest),
        "track": chosen.get("id"),
        "name": chosen.get("name"),
        "skipped": False,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Mix a pool track onto decorated portraits (not the decorate job)."
    )
    p.add_argument("--input", type=Path, help="Decorated *_portrait_decorated.mp4, or a folder of them")
    p.add_argument("--output", type=Path, help="Music mp4 (single --input file only)")
    p.add_argument("--dataset-id")
    p.add_argument("--dataset-dir", type=Path)
    p.add_argument("--output-root", type=Path, default=DATA)
    p.add_argument("--only", default="")
    p.add_argument(
        "--from-picks",
        action="store_true",
        help="Only mix lol_compilations_picks_portrait/",
    )
    p.add_argument(
        "--track",
        required=True,
        help="Pool track id (see music_pool.py). auto picks for multikill.",
    )
    p.add_argument("--music-db", type=float, default=-18.0)
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def resolve_dataset(args: argparse.Namespace) -> Path | None:
    if args.dataset_dir is not None:
        return args.dataset_dir.resolve()
    if args.dataset_id:
        found = find_dataset_dir(args.output_root, args.dataset_id)
        return (found or (args.output_root / args.dataset_id)).resolve()
    return None


def main() -> int:
    load_dotenv()
    args = parse_args()
    try:
        dataset_dir = resolve_dataset(args)
        jobs: list[Path] = []
        if args.input is not None:
            inp = args.input.resolve()
            if inp.is_dir():
                jobs = [p for p in sorted(inp.glob("*.mp4")) if is_decorated_portrait(p)]
                if args.only:
                    jobs = [p for p in jobs if game_only_matches(p.name, args.only)]
            else:
                jobs = [inp]
            if args.output is not None and (inp.is_dir() or len(jobs) != 1):
                print("--output is only valid with a single file --input.", file=sys.stderr)
                return 2
        elif dataset_dir is not None:
            if not dataset_dir.is_dir():
                raise FileNotFoundError(dataset_dir)
            jobs = discover_decorated(
                dataset_dir,
                only=str(args.only or ""),
                picks_only=bool(args.from_picks),
            )
        else:
            print("Pass --input or --dataset-dir / --dataset-id.", file=sys.stderr)
            return 2
        if not jobs:
            raise FileNotFoundError("No *_portrait_decorated.mp4 files to mix")

        results: list[dict[str, Any]] = []
        single = bool(args.input and args.input.is_file())
        for path in jobs:
            run_print(f"[music] {path.name}")
            results.append(
                mix_one(
                    path,
                    track_query=str(args.track),
                    output=args.output if single else None,
                    force=bool(args.force),
                    music_db=float(args.music_db),
                )
            )
        print(json.dumps({"status": "ok", "count": len(results), "results": results}, indent=2))
        return 0
    except Exception as exc:
        print(f"Music mix failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
