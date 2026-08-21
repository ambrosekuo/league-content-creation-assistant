#!/usr/bin/env python3
"""One-encode decorate: combos + captions + intro/outro in a single libx264 pass.

Does not replace decorate_portrait.py. Writes {stem}_portrait_once.mp4 next to
the dry portrait and leaves *_decorated.mp4 alone.

  python decorate_portrait_once.py --dataset-dir data/aug21_2026_2852149914 --from-picks --only gam01
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from asr import default_model_for, resolve_engine
from dataset_paths import find_dataset_dir, game_only_matches
from decorate_portrait import (
    CAM_FRACTION,
    COMBO_BAND_H,
    champion_from_name,
    discover_portraits,
    dry_portrait,
    ensure_combos,
    find_weave,
    weave_stem_for,
)
from env_loader import load_dotenv
from ffmpeg_color import VIDEO_TO_BT709, X264_BT709

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def run_print(msg: str) -> None:
    print(msg, flush=True)


def once_output_for(portrait: Path) -> Path:
    return portrait.with_name(f"{portrait.stem}_once.mp4")


def even(n: int) -> int:
    n = int(n)
    return n if n % 2 == 0 else n - 1


def probe_av(path: Path) -> dict[str, Any]:
    from render_rank_cards import _probe_av

    return _probe_av(path)


def prepare_captions_ass(
    source: Path,
    dest: Path,
    *,
    asr_engine: str,
    asr_model: str,
    force_transcribe: bool,
    work_dir: Path,
) -> Path | None:
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
        force=True,
        force_transcribe=force_transcribe,
        ass_only=True,
        keep_ass=True,
        work_dir=work_dir,
        music="off",
        asr_engine=asr_engine,
    )
    ass = Path(str(result.get("ass") or ""))
    return ass if ass.is_file() else None


def prepare_wrap_assets(
    weave: Path,
    work: Path,
    *,
    overlay_hold: float,
    end_seconds: float,
) -> dict[str, Any]:
    from PIL import Image

    from render_lobby_intro import resolve_lobby_assets
    from render_rank_cards import (
        DEFAULT_END_FACE,
        DEFAULT_FACE,
        DEFAULT_OVERLAY_STING,
        card_from_meta,
        default_end_sting,
        load_json,
        negative_lp,
        render_road_progress_overlay,
        write_clip,
    )

    _png, meta = resolve_lobby_assets(weave, None, None)
    if meta is None:
        run_print(f"[once] skip wrap: no lobby meta for {weave.name}")
        return {}

    name = weave_stem_for(weave) or weave.stem
    work.mkdir(parents=True, exist_ok=True)
    card = card_from_meta(load_json(meta), meta_path=meta)
    overlay_png = work / f"{name}_overlay_road_lp.png"
    render_road_progress_overlay(card).save(overlay_png)
    run_print(f"[once] overlay intro {overlay_png.name} ({overlay_hold:.1f}s)")

    end_mp4: Path | None = None
    end_face_path = DEFAULT_END_FACE if DEFAULT_END_FACE.is_file() else DEFAULT_FACE
    if not end_face_path.is_file():
        run_print("[once] skip outro: missing end-card face still")
    else:
        end_mp4 = work / f"{name}_end.mp4"
        end_face = Image.open(end_face_path).convert("RGBA")
        end_sting = default_end_sting(card)
        sting_note = f" sting={end_sting.name}" if end_sting else ""
        run_print(
            f"[once] end outro {end_mp4.name} ({end_seconds:.1f}s)"
            f"{' loss' if negative_lp(card) else ' win'}{sting_note}"
        )
        write_clip(
            card,
            end_face,
            end_mp4,
            kind="end",
            seconds=end_seconds,
            sting=end_sting,
        )

    sting = DEFAULT_OVERLAY_STING if DEFAULT_OVERLAY_STING.is_file() else None
    return {
        "overlay_png": overlay_png,
        "end_mp4": end_mp4,
        "sting": sting,
        "card": card,
        "meta": str(meta),
    }


def encode_once(
    source: Path,
    dest: Path,
    *,
    tiles: list[dict[str, Any]],
    ass_path: Path | None,
    fonts_dir: Path | None,
    overlay_png: Path | None,
    end_mp4: Path | None,
    sting: Path | None,
    overlay_hold: float,
    crf: int,
    preset: str,
) -> None:
    from caption_portraits import ffmpeg_filter_path

    info = probe_av(source)
    w = even(int(info["width"]))
    h = even(int(info["height"]))
    rate = float(info["fps"])
    dur = float(info["duration"])
    fade_in = 0.10
    fade_out = 0.40
    fade_out_at = max(0.0, float(overlay_hold) - fade_out)
    ov_t = max(0.2, float(overlay_hold) + 0.05)

    cmd = ["ffmpeg", "-y", "-hide_banner", "-i", str(source)]
    idx = 1
    tile_idxs: list[int] = []
    for tile in tiles:
        cmd += ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(tile["path"])]
        tile_idxs.append(idx)
        idx += 1

    intro_idx: int | None = None
    if overlay_png is not None:
        cmd += ["-loop", "1", "-t", f"{ov_t:.3f}", "-i", str(overlay_png)]
        intro_idx = idx
        idx += 1

    outro_idx: int | None = None
    outro_dur = 0.0
    if end_mp4 is not None:
        cmd += ["-i", str(end_mp4)]
        outro_idx = idx
        idx += 1
        outro_dur = float(probe_av(end_mp4)["duration"])

    sting_idx: int | None = None
    sting_ok = sting is not None and sting.is_file()
    if sting_ok:
        cmd += ["-i", str(sting)]
        sting_idx = idx

    parts: list[str] = []
    last = "[0:v]"
    for i, tile in enumerate(tiles):
        t0 = float(tile["t0"])
        t1 = float(tile["t1"])
        src_i = tile_idxs[i]
        tag = f"c{i}"
        parts.append(
            f"[{src_i}:v]format=rgba,fade=t=in:st={t0:.3f}:d=0.08:alpha=1,"
            f"fade=t=out:st={max(t0, t1 - 0.35):.3f}:d=0.35:alpha=1[ov{i}];"
            f"{last}[ov{i}]overlay=x={int(tile['x'])}:y={int(tile['y'])}:"
            f"enable='between(t,{t0:.3f},{t1:.3f})'[{tag}];"
        )
        last = f"[{tag}]"

    if ass_path is not None and fonts_dir is not None:
        ass = ffmpeg_filter_path(ass_path)
        fonts = ffmpeg_filter_path(fonts_dir)
        parts.append(f"{last}subtitles='{ass}':fontsdir='{fonts}'[cap];")
        last = "[cap]"

    if intro_idx is not None:
        parts.append(
            f"[{intro_idx}:v]format=rgba,fade=t=in:st=0:d={fade_in:.3f}:alpha=1,"
            f"fade=t=out:st={fade_out_at:.3f}:d={fade_out:.3f}:alpha=1[intro];"
            f"{last}[intro]overlay=0:0:format=auto:eof_action=pass[ovp];"
        )
        last = "[ovp]"

    finish = (
        f"setpts=PTS-STARTPTS,"
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x0A0C12,"
        f"fps={rate:.3f},setsar=1,{VIDEO_TO_BT709}"
    )
    if outro_idx is None:
        parts.append(f"{last}{finish}[v]")
        vmap, amap = "[v]", "[a]"
        if info["has_audio"]:
            game_src = (
                "[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                "aresample=async=1:first_pts=0[game];"
            )
        else:
            game_src = f"anullsrc=r=48000:cl=stereo:d={max(0.05, dur):.3f}[game];"
        if sting_ok and sting_idx is not None:
            parts.append(
                f"{game_src}"
                f"[{sting_idx}:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                f"volume=14dB[sting];"
                f"[game][sting]amix=inputs=2:duration=first:normalize=0:dropout_transition=0.15[a]"
            )
        else:
            parts.append(game_src.replace("[game];", "[a];"))
        out_t = max(0.2, dur + 0.05)
    else:
        parts.append(f"{last}{finish}[v0];")
        if info["has_audio"]:
            game_src = (
                "[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                "aresample=async=1:first_pts=0[game];"
            )
        else:
            game_src = f"anullsrc=r=48000:cl=stereo:d={max(0.05, dur):.3f}[game];"
        if sting_ok and sting_idx is not None:
            game_a = (
                f"{game_src}"
                f"[{sting_idx}:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                f"volume=14dB[sting];"
                f"[game][sting]amix=inputs=2:duration=first:normalize=0:dropout_transition=0.15[a0];"
            )
        else:
            game_a = game_src.replace("[game];", "[a0];")
        parts.append(
            f"{game_a}"
            f"[{outro_idx}:v]setpts=PTS-STARTPTS,"
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x0A0C12,"
            f"fps={rate:.3f},setsar=1,{VIDEO_TO_BT709}[v1];"
            f"[{outro_idx}:a]asetpts=PTS-STARTPTS,"
            f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a1];"
            f"[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]"
        )
        vmap, amap = "[v]", "[a]"
        out_t = max(0.2, dur + outro_dur + 0.25)

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.stem}._once.mp4")
    cmd += [
        "-filter_complex",
        "".join(parts),
        "-map",
        vmap,
        "-map",
        amap,
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        *X264_BT709,
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-t",
        f"{out_t:.3f}",
        "-movflags",
        "+faststart",
        str(tmp),
    ]
    run_print(f"[once] encode {source.name} → {dest.name} ({len(tiles)} combo tiles)")
    proc = subprocess.run(cmd, text=True)
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg once-encode failed ({proc.returncode})")
    tmp.replace(dest)


def decorate_once(
    portrait: Path,
    *,
    weave: Path | None,
    output: Path | None,
    dataset_dir: Path | None,
    force: bool,
    force_combos: bool,
    force_transcribe: bool,
    skip_combos: bool,
    skip_captions: bool,
    skip_wrap: bool,
    overlay_hold: float,
    end_seconds: float,
    cam_fraction: float,
    preset: str,
    crf: int,
    keep_work: bool,
    asr_engine: str,
    asr_model: str,
) -> dict[str, Any]:
    from caption_portraits import _first_font
    from combo_detector import prepare_overlay_tiles, probe

    portrait = portrait.resolve()
    dest = (output or once_output_for(portrait)).resolve()
    if dest.is_file() and not force:
        run_print(f"[skip] exists {dest.name} (pass --force)")
        return {"source": str(portrait), "output": str(dest), "skipped": True}

    weave_path = weave or find_weave(portrait, dataset_dir)
    work_root = dest.parent / f".{portrait.stem}_once"
    if work_root.exists():
        shutil.rmtree(work_root, ignore_errors=True)
    work_root.mkdir(parents=True, exist_ok=True)
    steps: dict[str, Any] = {"encodes": 1}

    try:
        tiles: list[dict[str, Any]] = []
        if not skip_combos:
            if weave_path is None:
                run_print("[once] skip combos: no landscape weave")
            else:
                champ = champion_from_name(weave_path.name)
                payload = ensure_combos(weave_path, champion=champ, force=force_combos)
                if payload:
                    combos = [
                        c
                        for c in (payload.get("combos") or [])
                        if len(c.get("events") or []) >= 2
                    ]
                    if not combos:
                        run_print("[once] skip combos: no 2+ spell chains")
                    else:
                        info = probe(portrait)
                        tiles = prepare_overlay_tiles(
                            {**payload, "combos": combos},
                            work_root / "combo_tiles",
                            src_w=int(info["width"]),
                            src_h=int(info["height"]),
                            hold=2.2,
                            band_y=int(round(int(info["height"]) * float(cam_fraction))),
                            band_h=COMBO_BAND_H,
                            fill=0.8,
                        )
                        steps["combos"] = len(tiles)
                        run_print(f"[once] {len(tiles)} combo tiles")

        ass_path: Path | None = None
        fonts_dir: Path | None = None
        if not skip_captions:
            ass_path = prepare_captions_ass(
                portrait,
                dest,
                asr_engine=asr_engine,
                asr_model=asr_model,
                force_transcribe=force_transcribe,
                work_dir=work_root / "caption",
            )
            if ass_path is not None:
                fonts_dir = _first_font()[0].parent
                steps["captions"] = str(ass_path)

        overlay_png: Path | None = None
        end_mp4: Path | None = None
        sting: Path | None = None
        if not skip_wrap:
            if weave_path is None:
                run_print("[once] skip wrap: no landscape weave / lobby meta")
            else:
                wrap = prepare_wrap_assets(
                    weave_path,
                    work_root / "wrap",
                    overlay_hold=overlay_hold,
                    end_seconds=end_seconds,
                )
                overlay_png = wrap.get("overlay_png")
                end_mp4 = wrap.get("end_mp4")
                sting = wrap.get("sting")
                if overlay_png or end_mp4:
                    steps["wrap"] = {
                        "overlay": str(overlay_png) if overlay_png else None,
                        "end": str(end_mp4) if end_mp4 else None,
                    }

        encode_once(
            portrait,
            dest,
            tiles=tiles,
            ass_path=ass_path,
            fonts_dir=fonts_dir,
            overlay_png=overlay_png,
            end_mp4=end_mp4,
            sting=sting,
            overlay_hold=overlay_hold,
            crf=crf,
            preset=preset,
        )
        run_print(f"[once] {portrait.name} → {dest.name}")
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
            if dest:
                dest.with_name(f"{portrait.stem}_captions.ass").unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Decorate a dry portrait in one encode (does not replace decorate_portrait.py)."
    )
    p.add_argument("--input", type=Path, help="Dry *_portrait.mp4, or a folder of them")
    p.add_argument("--output", type=Path, help="Once mp4 (single --input file only)")
    p.add_argument("--weave", type=Path, help="Landscape weave used for combo detect + lobby meta")
    p.add_argument("--dataset-id")
    p.add_argument("--dataset-dir", type=Path)
    p.add_argument("--output-root", type=Path, default=DATA)
    p.add_argument("--only", default="")
    p.add_argument(
        "--from-picks",
        action="store_true",
        help="Only decorate lol_compilations_picks_portrait/",
    )
    p.add_argument("--force", action="store_true")
    p.add_argument("--force-combos", action="store_true")
    p.add_argument("--force-transcribe", action="store_true")
    p.add_argument("--skip-combos", action="store_true")
    p.add_argument("--skip-captions", action="store_true")
    p.add_argument("--skip-wrap", action="store_true")
    p.add_argument(
        "--asr",
        dest="asr_engine",
        default=None,
        choices=("whisper", "openai"),
        help="Caption ASR. Default: $CAPTION_ASR or openai",
    )
    p.add_argument("--asr-model", default=None)
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
        if found is None:
            raise FileNotFoundError(f"No dataset for id {args.dataset_id}")
        return found
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
            run_print(f"[once] {path}")
            results.append(
                decorate_once(
                    path,
                    weave=args.weave.resolve() if args.weave else None,
                    output=args.output if single else None,
                    dataset_dir=dataset_dir,
                    force=bool(args.force),
                    force_combos=bool(args.force_combos),
                    force_transcribe=bool(args.force_transcribe),
                    skip_combos=bool(args.skip_combos),
                    skip_captions=bool(args.skip_captions),
                    skip_wrap=bool(args.skip_wrap),
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
        print(f"Decorate-once failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
