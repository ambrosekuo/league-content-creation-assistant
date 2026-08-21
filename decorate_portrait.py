#!/usr/bin/env python3
"""Decorate a dry 9:16 portrait: combos → captions → intro/outro.

The portrait job (render_portrait.py / process-portraits) now emits layout
only. This step adds the social-export layer. Music is a later step
(`mix_portrait_music.py`).

  python decorate_portrait.py --input data/.../gam14_..._portrait.mp4
  python decorate_portrait.py --dataset-dir data/aug17_2026_2849217240 --only gam14

Writes {stem}_decorated.mp4 next to the dry portrait.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from asr import default_model_for, resolve_engine
from dataset_paths import find_dataset_dir, game_only_matches
from env_loader import load_dotenv

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

PORTRAIT_DIRS = (
    ("lol_compilations_picks_portrait", "lol_compilations_picks"),
    ("lol_compilations_portrait", "lol_compilations"),
)
CAM_FRACTION = 0.34
COMBO_BAND_H = 110


def run_print(msg: str) -> None:
    print(msg, flush=True)


def dry_portrait(path: Path) -> bool:
    stem = path.stem.lower()
    if path.suffix.lower() != ".mp4" or not path.is_file():
        return False
    if not stem.endswith("_portrait"):
        return False
    skip = ("_captioned", "_decorated", "_pool", "_music", "_nomusic", "lobby", "preview")
    return not any(bit in stem for bit in skip)


def weave_stem_for(portrait: Path) -> str:
    stem = portrait.stem
    if stem.endswith("_portrait"):
        return stem[: -len("_portrait")]
    return stem


def find_weave(portrait: Path, dataset_dir: Path | None) -> Path | None:
    stem = weave_stem_for(portrait)
    candidates: list[Path] = [portrait.parent.parent / "lol_compilations_picks" / f"{stem}.mp4"]
    if dataset_dir is not None:
        candidates.extend(
            [
                dataset_dir / "lol_compilations_picks" / f"{stem}.mp4",
                dataset_dir / "lol_compilations" / f"{stem}.mp4",
            ]
        )
        parent = portrait.parent.name
        for portrait_dir, weave_dir in PORTRAIT_DIRS:
            if parent == portrait_dir:
                candidates.insert(0, portrait.parent.parent / weave_dir / f"{stem}.mp4")
    else:
        candidates.append(portrait.parent.parent / "lol_compilations" / f"{stem}.mp4")
    seen: set[Path] = set()
    for path in candidates:
        key = path.resolve() if path.exists() else path
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            return path.resolve()
    return None


def champion_from_name(name: str) -> str:
    from combo_detector import CHAMPIONS

    parts = Path(name).stem.lower().replace("-", "_").split("_")
    for part in parts:
        if part in CHAMPIONS:
            return part
    return "leblanc"


def discover_portraits(dataset_dir: Path, *, only: str, picks_only: bool = False) -> list[Path]:
    found: list[Path] = []
    dirs = PORTRAIT_DIRS[:1] if picks_only else PORTRAIT_DIRS
    for portrait_dir, _weave_dir in dirs:
        folder = dataset_dir / portrait_dir
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.mp4")):
            if not dry_portrait(path):
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


def ensure_combos(
    weave: Path,
    *,
    champion: str,
    force: bool,
) -> dict[str, Any] | None:
    from combo_detector import detect_casts, parse_summoners

    json_path = weave.with_name(f"{weave.stem}_combo.json")
    if json_path.is_file() and not force:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        run_print(f"[decorate] reuse {json_path.name} ({len(payload.get('combos') or [])} combos)")
        return payload
    run_print(f"[decorate] detect combos on {weave.name}")
    try:
        payload = detect_casts(
            weave,
            fps=20.0,
            champion_id=champion,
            summoners=parse_summoners("FLASH,IGNITE"),
            gap=1.8,
            include_recast=True,
        )
    except Exception as exc:
        run_print(f"[decorate] skip combos: {exc}")
        return None
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    n = len(payload.get("combos") or [])
    run_print(f"[decorate] {n} combos → {json_path.name}")
    return payload


def overlay_combos(
    portrait: Path,
    payload: dict[str, Any],
    dest: Path,
    *,
    cam_fraction: float,
) -> bool:
    from combo_detector import probe, render_overlay

    combos = [
        c
        for c in (payload.get("combos") or [])
        if len(c.get("events") or []) >= 2
    ]
    if not combos:
        run_print("[decorate] skip combos: no 2+ spell chains")
        return False
    payload = {**payload, "combos": combos}
    info = probe(portrait)
    band_y = int(round(int(info["height"]) * float(cam_fraction)))
    render_overlay(
        portrait,
        payload,
        dest,
        hold=2.2,
        band_y=band_y,
        band_h=COMBO_BAND_H,
        fill=0.8,
    )
    return True


def burn_captions(
    source: Path,
    dest: Path,
    *,
    force: bool,
    asr_engine: str,
    asr_model: str,
) -> Path:
    from caption_portraits import caption_one

    result = caption_one(
        source,
        output=dest,
        model_name=asr_model,
        language="en",
        to_lang="",
        device="cpu",
        compute_type="int8",
        case_mode="upper",
        max_words=3,
        max_gap=0.42,
        max_dur=2.3,
        skip_head=0.0,
        skip_tail=0.0,
        preset="veryfast",
        crf=20,
        force=force,
        force_transcribe=False,
        ass_only=False,
        keep_ass=False,
        work_dir=None,
        music="off",
        asr_engine=asr_engine,
    )
    out = Path(str(result.get("output") or dest))
    return out if out.is_file() else dest


def wrap_intro_outro(
    source: Path,
    weave: Path,
    dest: Path,
    *,
    overlay_hold: float,
    end_seconds: float,
    crf: int,
    preset: str,
) -> bool:
    from render_lobby_intro import resolve_lobby_assets
    from render_rank_cards import wrap_portrait

    _png, meta = resolve_lobby_assets(weave, None, None)
    if meta is None:
        run_print(f"[decorate] skip wrap: no lobby meta for {weave.name}")
        return False
    wrap_portrait(
        source,
        meta,
        output=dest,
        intro=True,
        outro=True,
        overlay_hold=float(overlay_hold),
        end_seconds=float(end_seconds),
        crf=int(crf),
        preset=str(preset),
    )
    return dest.is_file()


def mix_music(source: Path, dest: Path, *, music: str, music_db: float) -> bool:
    from caption_portraits import mix_music_bed

    report = mix_music_bed(source, music, music_db=float(music_db))
    if not report:
        return False
    mixed = Path(str(report["output"]))
    if mixed.resolve() != dest.resolve():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(mixed, dest)
        mixed.unlink(missing_ok=True)
    run_print(f"[decorate] music {report.get('id')} → {dest.name}")
    return dest.is_file()


def decorate_one(
    portrait: Path,
    *,
    weave: Path | None,
    output: Path | None,
    dataset_dir: Path | None,
    force: bool,
    force_combos: bool,
    skip_combos: bool,
    skip_captions: bool,
    skip_wrap: bool,
    music: str,
    music_db: float,
    overlay_hold: float,
    end_seconds: float,
    cam_fraction: float,
    preset: str,
    crf: int,
    keep_work: bool,
    asr_engine: str,
    asr_model: str,
) -> dict[str, Any]:
    portrait = portrait.resolve()
    if not dry_portrait(portrait) and not force:
        # Allow decorating a named dry file even if the helper is strict.
        if not portrait.is_file():
            raise FileNotFoundError(portrait)
    dest = (output or portrait.with_name(f"{portrait.stem}_decorated.mp4")).resolve()
    if dest.is_file() and not force:
        run_print(f"[skip] exists {dest.name} (pass --force)")
        return {"source": str(portrait), "output": str(dest), "skipped": True}

    weave_path = weave or find_weave(portrait, dataset_dir)
    work_root = dest.parent / f".{portrait.stem}_decorate"
    if work_root.exists():
        shutil.rmtree(work_root, ignore_errors=True)
    work_root.mkdir(parents=True, exist_ok=True)
    current = portrait
    steps: dict[str, Any] = {}

    try:
        if not skip_combos:
            if weave_path is None:
                run_print("[decorate] skip combos: no landscape weave")
            else:
                champ = champion_from_name(weave_path.name)
                payload = ensure_combos(weave_path, champion=champ, force=force_combos)
                if payload:
                    combo_mp4 = work_root / "combo.mp4"
                    if overlay_combos(
                        current, payload, combo_mp4, cam_fraction=cam_fraction
                    ):
                        current = combo_mp4
                        steps["combos"] = str(combo_mp4)

        if not skip_captions:
            cap_mp4 = work_root / "captioned.mp4"
            current = burn_captions(
                current,
                cap_mp4,
                force=True,
                asr_engine=asr_engine,
                asr_model=asr_model,
            )
            steps["captions"] = str(current)

        if not skip_wrap:
            if weave_path is None:
                run_print("[decorate] skip wrap: no landscape weave / lobby meta")
            else:
                wrap_mp4 = work_root / "wrapped.mp4"
                if wrap_intro_outro(
                    current,
                    weave_path,
                    wrap_mp4,
                    overlay_hold=overlay_hold,
                    end_seconds=end_seconds,
                    crf=crf,
                    preset=preset,
                ):
                    current = wrap_mp4
                    steps["wrap"] = str(wrap_mp4)

        dest.parent.mkdir(parents=True, exist_ok=True)
        mixed = False
        if str(music or "off").strip().lower() not in {"", "off", "none", "false", "0"}:
            mixed = mix_music(current, dest, music=music, music_db=music_db)
            if mixed:
                steps["music"] = str(dest)
        if not mixed:
            if current.resolve() == dest.resolve():
                pass
            else:
                shutil.copy2(current, dest)
        run_print(f"[decorate] {portrait.name} → {dest.name}")
        return {
            "source": str(portrait),
            "weave": str(weave_path) if weave_path else None,
            "output": str(dest),
            "steps": steps,
            "skipped": False,
        }
    finally:
        if not keep_work:
            shutil.rmtree(work_root, ignore_errors=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Decorate a dry portrait: combos, captions, intro/outro (no music)."
    )
    p.add_argument("--input", type=Path, help="Dry *_portrait.mp4, or a folder of them")
    p.add_argument("--output", type=Path, help="Decorated mp4 (single --input file only)")
    p.add_argument("--weave", type=Path, help="Landscape weave used for combo detect + lobby meta")
    p.add_argument("--dataset-id")
    p.add_argument("--dataset-dir", type=Path)
    p.add_argument("--output-root", type=Path, default=DATA)
    p.add_argument("--only", default="")
    p.add_argument(
        "--from-picks",
        action="store_true",
        help="Only decorate lol_compilations_picks_portrait/ (reviewed weaves)",
    )
    p.add_argument("--force", action="store_true")
    p.add_argument("--force-combos", action="store_true", help="Redo combo detect even if JSON exists")
    p.add_argument("--skip-combos", action="store_true")
    p.add_argument("--skip-captions", action="store_true")
    p.add_argument(
        "--asr",
        dest="asr_engine",
        default=None,
        choices=("whisper", "openai"),
        help="Caption ASR. Default: $CAPTION_ASR or openai",
    )
    p.add_argument(
        "--asr-model",
        default=None,
        help="Caption ASR model. Default: gpt-4o-mini-transcribe (openai) or small.en",
    )
    p.add_argument("--skip-wrap", action="store_true")
    p.add_argument(
        "--music",
        default="off",
        help="Leave off (default). Music is mix_portrait_music.py, not this job.",
    )
    p.add_argument("--music-db", type=float, default=-18.0)
    p.add_argument("--overlay-hold", type=float, default=2.0)
    p.add_argument("--end-seconds", type=float, default=2.5)
    p.add_argument("--cam-fraction", type=float, default=CAM_FRACTION)
    p.add_argument("--preset", default="veryfast")
    p.add_argument("--crf", type=int, default=20)
    p.add_argument("--keep-work", action="store_true")
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
                jobs = [p for p in sorted(inp.glob("*.mp4")) if dry_portrait(p)]
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
            jobs = discover_portraits(
                dataset_dir,
                only=str(args.only or ""),
                picks_only=bool(args.from_picks),
            )
        else:
            print("Pass --input or --dataset-dir / --dataset-id.", file=sys.stderr)
            return 2
        if not jobs:
            raise FileNotFoundError("No dry *_portrait.mp4 files to decorate")

        results: list[dict[str, Any]] = []
        single = bool(args.input and args.input.is_file())
        asr_engine = resolve_engine(
            args.asr_engine or os.environ.get("CAPTION_ASR"),
            default="openai",
        )
        asr_model = str(args.asr_model or default_model_for(asr_engine))
        for path in jobs:
            run_print(f"[decorate] {path}")
            results.append(
                decorate_one(
                    path,
                    weave=args.weave.resolve() if args.weave else None,
                    output=args.output if single else None,
                    dataset_dir=dataset_dir,
                    force=bool(args.force),
                    force_combos=bool(args.force_combos),
                    skip_combos=bool(args.skip_combos),
                    skip_captions=bool(args.skip_captions),
                    skip_wrap=bool(args.skip_wrap),
                    music=str(args.music),
                    music_db=float(args.music_db),
                    overlay_hold=float(args.overlay_hold),
                    end_seconds=float(args.end_seconds),
                    cam_fraction=float(args.cam_fraction),
                    preset=str(args.preset),
                    crf=int(args.crf),
                    keep_work=bool(args.keep_work),
                    asr_engine=asr_engine,
                    asr_model=asr_model,
                )
            )
        print(json.dumps({"status": "ok", "count": len(results), "results": results}, indent=2))
        return 0
    except Exception as exc:
        print(f"Decorate failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
