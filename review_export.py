#!/usr/bin/env python3
"""Local workflow: stitch reviewed clips, then render 9:16 portraits.

Ratings live in data/_viewer/{day}_{vodId}/ — not copies of the mp4s.
The videos stay in data/{day}_{vodId}/lol_clips/. This script reads
approved/godly.json and excellent.json, stitches one weave per
game into lol_compilations_picks/, then portraits next to that.

  python review_export.py --dataset-dir data/aug17_2026_2849217240
  python review_export.py --vod-id 2849217240 --rating godly,excellent
  python review_export.py --dataset-dir data/aug17_2026_2849217240 --skip-portrait
  python review_export.py --dataset-dir data/aug17_2026_2849217240 --only g02 --force
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from dataset_paths import find_dataset_dir, game_only_matches, vod_id_from_dir_name
from env_loader import load_dotenv

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stitch reviewed clips and optionally render portraits")
    p.add_argument("--dataset-dir", type=Path, default=None)
    p.add_argument("--vod-id", default="")
    p.add_argument(
        "--rating",
        default="godly,excellent",
        help="Approved queues to stitch (default: godly,excellent)",
    )
    p.add_argument("--only", default="", help="Only games whose folder/name contains this")
    p.add_argument("--force", action="store_true", help="Overwrite existing weaves and portraits")
    p.add_argument("--skip-portrait", action="store_true", help="Stop after landscape weaves")
    p.add_argument(
        "--portrait-only",
        action="store_true",
        help="Skip stitch; re-render portraits from existing picks weaves",
    )
    p.add_argument("--portrait", action="store_true", help="Render portraits (default: on unless --skip-portrait)")
    p.add_argument("--preset", default="veryfast")
    p.add_argument("--crf", default="20")
    p.add_argument("--game-zoom", default="0.65")
    p.add_argument("--no-track-champion", action="store_true")
    p.add_argument(
        "--music",
        default="off",
        help="Portrait music bed (default: off; mix_portrait_music.py is a later step)",
    )
    p.add_argument(
        "--decorate",
        action="store_true",
        help="After dry portraits, run decorate_portrait.py (combos/captions/wrap, no music)",
    )
    return p.parse_args()


def resolve_dataset(args: argparse.Namespace) -> Path:
    if args.dataset_dir:
        path = args.dataset_dir.resolve()
        if not path.is_dir():
            raise SystemExit(f"missing dataset dir {path}")
        return path
    vid = (args.vod_id or "").strip().lstrip("v")
    if not vid:
        raise SystemExit("pass --dataset-dir or --vod-id")
    found = find_dataset_dir(DATA, vid)
    if found is None:
        raise SystemExit(f"no local dataset for vod {vid} under {DATA}")
    return found.resolve()


def game_folder_for_weave(weave: Path, stitch_dir: Path, dataset_dir: Path) -> str | None:
    report = stitch_dir / "compilations.json"
    if report.is_file():
        payload = json.loads(report.read_text(encoding="utf-8"))
        for row in payload.get("weaves") or []:
            if str(row.get("filename") or "") == weave.name:
                folder = row.get("gameFolder")
                if folder:
                    return str(folder)
    try:
        game_index = int(weave.stem.split("_", 1)[0].replace("gam", ""))
    except ValueError:
        return None
    clips_dir = dataset_dir / "lol_clips"
    if not clips_dir.is_dir():
        return None
    prefix = f"g{game_index:02d}_"
    for path in sorted(clips_dir.iterdir()):
        if path.is_dir() and path.name.startswith(prefix):
            return path.name
    return None


def ensure_lobby_sidecars(weave: Path, dataset_dir: Path) -> bool:
    """Ensure overlay/outro lobby PNG + meta sit next to the picks weave."""
    meta_path = weave.with_name(f"{weave.stem}_lobby_meta.json")
    png_path = weave.with_name(f"{weave.stem}_lobby.png")
    if meta_path.is_file() and png_path.is_file():
        return True

    src_comp = dataset_dir / "lol_compilations"
    src_png = src_comp / png_path.name
    src_meta = src_comp / meta_path.name
    if src_png.is_file() and src_meta.is_file():
        shutil.copy2(src_png, png_path)
        shutil.copy2(src_meta, meta_path)
        print(f"[lobby] copied from lol_compilations → {weave.name}", flush=True)
        return True

    if not os.environ.get("RIOT_API_KEY", "").strip():
        print(
            f"[lobby] skip {weave.name}: no sidecar (set RIOT_API_KEY to generate)",
            flush=True,
        )
        return False

    from stitch_game_clips import generate_lobby_png, match_id_for_folder

    game_folder = game_folder_for_weave(weave, weave.parent, dataset_dir)
    if not game_folder:
        print(f"[lobby] skip {weave.name}: no game folder", flush=True)
        return False
    match_id = match_id_for_folder(game_folder, dataset_dir, None)
    if not match_id:
        print(f"[lobby] skip {weave.name}: no matchId for {game_folder}", flush=True)
        return False
    print(f"[lobby] generate {weave.name} match={match_id}", flush=True)
    generate_lobby_png(match_id, png_path)
    return meta_path.is_file() and png_path.is_file()


def portrait_weaves(comp_dir: Path, only: str) -> list[Path]:
    candidates = list(comp_dir.glob("gam*.mp4")) + list(comp_dir.glob("*_weave.mp4"))
    weaves = sorted(
        {
            p
            for p in candidates
            if p.is_file()
            and "lobby" not in p.name.lower()
            and not p.name.endswith("_portrait.mp4")
        },
        key=lambda p: p.name,
    )
    if only:
        weaves = [p for p in weaves if game_only_matches(p.name, only)]
    return weaves


def render_portraits(
    weaves: list[Path],
    out_dir: Path,
    dataset_dir: Path,
    *,
    force: bool,
    preset: str,
    crf: str,
    game_zoom: str,
    track_champion: bool,
    music: str = "off",
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for weave in weaves:
        out_mp4 = out_dir / f"{weave.stem}_portrait.mp4"
        preview = out_dir / f"{weave.stem}_portrait_preview.jpg"
        if out_mp4.exists() and not force:
            print(f"[skip] exists {out_mp4.name} (pass --force)", flush=True)
            continue
        ensure_lobby_sidecars(weave, dataset_dir)
        cmd = [
            sys.executable,
            str(ROOT / "render_portrait.py"),
            "--input",
            str(weave),
            "--output",
            str(out_mp4),
            "--preview-frame",
            str(preview),
            "--intro",
            "none",
            "--no-outro",
            "--game-zoom",
            str(game_zoom),
            "--cam-hole",
            "fill",
            "--preset",
            str(preset),
            "--crf",
            str(crf),
            "--kda-overlay",
            "--music",
            str(music or "off"),
        ]
        if track_champion:
            cmd.append("--track-champion")
        lobby_png = weave.with_name(f"{weave.stem}_lobby.png")
        lobby_meta = weave.with_name(f"{weave.stem}_lobby_meta.json")
        if lobby_png.is_file():
            cmd += ["--lobby-png", str(lobby_png)]
        if lobby_meta.is_file():
            cmd += ["--lobby-meta", str(lobby_meta)]
        print(f"[portrait] {weave.name} → {out_mp4.name}", flush=True)
        run(cmd)


def main() -> int:
    args = parse_args()
    load_dotenv()
    dataset_dir = resolve_dataset(args)
    ratings = [part.strip().lower() for part in str(args.rating).split(",") if part.strip()]
    tag = "_".join(ratings) or "godly_excellent"
    stitch_dir = dataset_dir / "lol_compilations_picks"
    portrait_dir = dataset_dir / "lol_compilations_picks_portrait"

    if not args.portrait_only:
        stitch_cmd = [
            sys.executable,
            str(ROOT / "stitch_game_clips.py"),
            "--dataset-dir",
            str(dataset_dir),
            "--from-approved",
            ",".join(ratings),
            "--output-dir",
            str(stitch_dir),
        "--min-clips",
        "1",
        "--no-rank",
        "--reencode",
    ]
        if args.only:
            stitch_cmd += ["--only", args.only]
        if args.force:
            stitch_cmd.append("--force")
        print(f"[export] {vod_id_from_dir_name(dataset_dir.name)} rating={tag}", flush=True)
        run(stitch_cmd)
    else:
        print(f"[export] portrait-only {vod_id_from_dir_name(dataset_dir.name)}", flush=True)

    if args.skip_portrait and not args.portrait:
        print(f"[done] weaves → {stitch_dir}", flush=True)
        return 0

    weaves = portrait_weaves(stitch_dir, args.only)
    if not weaves:
        print(f"error: no weaves in {stitch_dir}", file=sys.stderr)
        return 1
    render_portraits(
        weaves,
        portrait_dir,
        dataset_dir,
        force=bool(args.force),
        preset=str(args.preset),
        crf=str(args.crf),
        game_zoom=str(args.game_zoom),
        track_champion=not args.no_track_champion,
        music=str(args.music),
    )
    print(f"[done] weaves → {stitch_dir}", flush=True)
    print(f"[done] portraits → {portrait_dir}", flush=True)
    if args.decorate:
        decorate_cmd = [
            sys.executable,
            str(ROOT / "decorate_portrait.py"),
            "--dataset-dir",
            str(dataset_dir),
            "--music",
            "off",
        ]
        if args.only:
            decorate_cmd += ["--only", args.only]
        if args.force:
            decorate_cmd.append("--force")
        print("[export] decorate portraits", flush=True)
        run(decorate_cmd)
        print(f"[done] decorated → {portrait_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
