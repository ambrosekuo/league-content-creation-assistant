#!/usr/bin/env python3
"""Weave per-game LoL clips into one compilation video per game folder.

Ranks clips and keeps the top K per game (default 5) unless --top-k 0.
Optionally prepends a lobby-card intro (default 3s) generated from Riot ranks.
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

from clip_edge_pad import PAD_LEAD_S, PAD_TRAIL_S, detect_edge_freezes

ROOT = Path(__file__).resolve().parent


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Missing file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{detail}")


def discover_games(clips_dir: Path) -> list[tuple[str, list[Path]]]:
    """Return [(game_folder_name, ordered clip paths), ...] from disk."""
    games: list[tuple[str, list[Path]]] = []
    for folder in sorted(p for p in clips_dir.iterdir() if p.is_dir()):
        clips = sorted(folder.glob("c*.mp4"))
        if clips:
            games.append((folder.name, clips))
    return games


def apply_top_k(
    games: list[tuple[str, list[Path]]],
    *,
    dataset_dir: Path,
    clips_dir: Path,
    top_k: int,
    extra_dirs: list[Path] | None = None,
) -> tuple[list[tuple[str, list[Path]]], dict[str, Any]]:
    """Rank clips and keep the best top_k per game, still in chrono order."""
    from score_clips import pick_top_k, rank_dataset, write_rank_outputs

    scored = rank_dataset(dataset_dir, clips_dir)
    if not scored:
        return games, {"top_k": top_k, "picked": 0, "scored": 0}
    picks = pick_top_k(scored, top_k, per_game=True)
    write_rank_outputs(
        dataset_dir=dataset_dir,
        clips_dir=clips_dir,
        scored=scored,
        picks=picks,
        top_k=top_k,
        extra_dirs=extra_dirs,
        per_game=True,
        scope="game",
    )
    keep_by_folder: dict[str, set[str]] = {}
    for clip in picks:
        folder = str(clip.get("gameFolder") or Path(clip["relativePath"]).parent.name)
        keep_by_folder.setdefault(folder, set()).add(Path(clip["relativePath"]).name)

    filtered: list[tuple[str, list[Path]]] = []
    for folder, clips in games:
        names = keep_by_folder.get(folder)
        if not names:
            print(f"[rank] {folder}: no picks, skipping", flush=True)
            continue
        kept = [p for p in clips if p.name in names]
        print(
            f"[rank] {folder}: {len(clips)} → {len(kept)} (top {top_k})",
            flush=True,
        )
        if kept:
            filtered.append((folder, kept))
    return filtered, {
        "top_k": top_k,
        "scored": len(scored),
        "picked": len(picks),
        "files": [c.get("relativePath") for c in picks],
        "scope": "game",
        "per_game": True,
    }


def apply_daily_top_k(
    *,
    dataset_dir: Path,
    clips_dir: Path,
    top_k: int,
    max_per_game: int,
    order: str,
    extra_dirs: list[Path] | None = None,
) -> tuple[list[Path], dict[str, Any]]:
    """Rank every clip under clips_dir and return the daily slate as paths."""
    from score_clips import pick_top_k, rank_dataset, write_rank_outputs

    scored = rank_dataset(dataset_dir, clips_dir)
    if not scored:
        return [], {"top_k": top_k, "picked": 0, "scored": 0, "scope": "daily"}
    picks = pick_top_k(
        scored,
        top_k,
        per_game=False,
        max_per_game=max_per_game,
        order=order,
    )
    write_rank_outputs(
        dataset_dir=dataset_dir,
        clips_dir=clips_dir,
        scored=scored,
        picks=picks,
        top_k=top_k,
        extra_dirs=extra_dirs,
        per_game=False,
        max_per_game=max_per_game,
        scope="daily",
    )
    paths = [Path(c["path"]) for c in picks if Path(c["path"]).is_file()]
    print(
        f"[rank] daily: {len(scored)} clips → {len(paths)} "
        f"(top {top_k}, max {max_per_game}/game, order={order})",
        flush=True,
    )
    for clip in picks:
        print(
            f"  #{clip.get('rank')}  {float(clip.get('score') or 0):.2f}  "
            f"{clip.get('relativePath')}",
            flush=True,
        )
    return paths, {
        "top_k": top_k,
        "max_per_game": max_per_game,
        "order": order,
        "scored": len(scored),
        "picked": len(paths),
        "files": [c.get("relativePath") for c in picks],
        "scope": "daily",
        "per_game": False,
        "enabled": True,
    }


def games_from_manifest(clips_dir: Path, manifest: dict[str, Any]) -> list[tuple[str, list[Path]]]:
    """Prefer clips.json order when present."""
    by_game: dict[str, list[tuple[int, Path]]] = {}
    for item in manifest.get("clips") or []:
        rel = item.get("relativePath") or item.get("filename")
        if not rel:
            continue
        path = clips_dir / rel
        if not path.is_file():
            continue
        folder = path.parent.name
        idx = int(item.get("clipIndexInGame") or item.get("index") or 0)
        by_game.setdefault(folder, []).append((idx, path))
    games: list[tuple[str, list[Path]]] = []
    for folder in sorted(by_game.keys()):
        ordered = [p for _, p in sorted(by_game[folder], key=lambda t: t[0])]
        if ordered:
            games.append((folder, ordered))
    return games or discover_games(clips_dir)


def match_id_for_folder(
    folder: str,
    dataset_dir: Path,
    manifest: dict[str, Any] | None,
) -> str | None:
    if manifest:
        for item in manifest.get("clips") or []:
            rel = str(item.get("relativePath") or item.get("filename") or "")
            if not rel.startswith(f"{folder}/"):
                continue
            mid = item.get("matchId")
            if mid:
                return str(mid)

    for name in ("lol_events_snapped.json", "lol_events.json"):
        path = dataset_dir / name
        if not path.is_file():
            continue
        payload = load_json(path)
        matches = list(payload.get("matches") or [])
        # Same naming as cut_lol_clips: chronological game index + champion
        # (+ optional _vsLaneOpponent).
        timed: list[tuple[float, dict[str, Any]]] = []
        for m in matches:
            offs = [
                float(e["vodOffsetSeconds"])
                for e in (m.get("events") or [])
                if e.get("vodOffsetSeconds") is not None
            ]
            timed.append((min(offs) if offs else 0.0, m))
        timed.sort(key=lambda t: t[0])
        for i, (_, m) in enumerate(timed, start=1):
            champ = "".join(ch for ch in str(m.get("champion") or "Unknown") if ch.isalnum()) or "Unknown"
            prefix = f"g{i:02d}_{champ}"
            if folder == prefix or folder.startswith(f"{prefix}_vs"):
                mid = m.get("matchId")
                return str(mid) if mid else None
    return None


def _safe_name(value: str | None, *, fallback: str = "unknown") -> str:
    text = "".join(ch for ch in str(value or "") if ch.isalnum())
    return (text or fallback).lower()


def match_meta_for_folder(
    folder: str,
    dataset_dir: Path,
    manifest: dict[str, Any] | None,
) -> dict[str, Any]:
    """Champion / lane opponent / win for weave naming."""
    mid = match_id_for_folder(folder, dataset_dir, manifest)
    meta: dict[str, Any] = {"matchId": mid, "champion": None, "laneOpponentChampion": None, "win": None}
    if manifest:
        for item in manifest.get("clips") or []:
            rel = str(item.get("relativePath") or "")
            if not rel.startswith(f"{folder}/"):
                continue
            meta["champion"] = item.get("champion") or meta["champion"]
            meta["laneOpponentChampion"] = (
                item.get("laneOpponentChampion") or meta["laneOpponentChampion"]
            )
            if item.get("win") is not None:
                meta["win"] = bool(item.get("win"))
            break
    if mid:
        for name in ("lol_events.json", "lol_events_snapped.json"):
            path = dataset_dir / name
            if not path.is_file():
                continue
            for m in load_json(path).get("matches") or []:
                if str(m.get("matchId")) != str(mid):
                    continue
                meta["champion"] = m.get("champion") or meta["champion"]
                meta["laneOpponentChampion"] = (
                    m.get("laneOpponentChampion") or meta["laneOpponentChampion"]
                )
                if m.get("win") is not None:
                    meta["win"] = bool(m.get("win"))
                return meta
    # Fallback parse folder g01_Leblanc_vsAhri
    parts = folder.split("_", 2)
    if len(parts) >= 2 and parts[0].startswith("g"):
        meta["champion"] = meta["champion"] or parts[1]
        if len(parts) >= 3 and parts[2].startswith("vs"):
            meta["laneOpponentChampion"] = meta["laneOpponentChampion"] or parts[2][2:]
    return meta


def weave_output_name(game_index: int, meta: dict[str, Any]) -> str:
    """e.g. gam01_leblanc_vs_ahri_win.mp4"""
    champ = _safe_name(meta.get("champion"), fallback="unknown")
    opp = _safe_name(meta.get("laneOpponentChampion"), fallback="unknown")
    result = "win" if meta.get("win") else "loss"
    return f"gam{game_index:02d}_{champ}_vs_{opp}_{result}.mp4"


def probe_video(path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,r_frame_rate",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        return {"width": 1920, "height": 1080, "fps": 30.0, "duration": 0.0}
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams") or [{}]
    s0 = streams[0] if streams else {}
    fps = 30.0
    rate = str(s0.get("r_frame_rate") or "30/1")
    if "/" in rate:
        num, den = rate.split("/", 1)
        try:
            fps = float(num) / max(float(den), 1.0)
        except ValueError:
            fps = 30.0
    try:
        duration = float((data.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "width": int(s0.get("width") or 1920),
        "height": int(s0.get("height") or 1080),
        "fps": max(1.0, min(fps, 60.0)),
        "duration": max(0.0, duration),
    }


def still_to_video(
    image: Path,
    output: Path,
    *,
    seconds: float,
    width: int,
    height: int,
    fps: float,
) -> Path:
    """Render lobby PNG as a short silent video matching clip geometry."""
    output.parent.mkdir(parents=True, exist_ok=True)
    # Even dims required by yuv420p
    width -= width % 2
    height -= height % 2
    seconds = max(0.1, float(seconds))
    fps = max(1.0, float(fps))
    frames = max(1, int(round(seconds * fps)))
    # Exact frame count + silent audio — avoids looped stills outliving -t.
    run(
        [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-framerate",
            f"{fps:.3f}",
            "-i",
            str(image),
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-vf",
            (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x0A0C12,"
                f"fps={fps:.3f},setsar=1"
            ),
            "-frames:v",
            str(frames),
            "-t",
            f"{seconds:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "stillimage",
            "-pix_fmt",
            "yuv420p",
            "-threads",
            "0",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-b:a",
            "160k",
            "-shortest",
            "-fflags",
            "+genpts",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    return output


def generate_lobby_png(match_id: str, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(ROOT / "generate_lobby_card.py"),
        "--match-id",
        match_id,
        "--output",
        str(output),
    ]
    highlight = (os.environ.get("RIOT_ID") or "").strip().strip('"')
    if highlight:
        cmd.extend(["--highlight", highlight])
    run(cmd)
    return output


def copy_trim_clip(
    src: Path,
    dst: Path,
    lead: float,
    trail: float,
    duration: float,
) -> None:
    """Drop start/end packets with -c copy (no encoder).

    ``-ss`` must be before ``-i`` so the copy starts on a keyframe. Output-side
    ``-ss`` keeps the previous GOP, which is the freeze we are trying to drop.
    """
    lead = max(0.0, float(lead))
    trail = max(0.0, float(trail))
    dur = max(0.0, float(duration))
    keep = dur - lead - trail if dur > 0 else 0.0
    if keep < 0.1:
        keep = max(0.1, dur - lead) if dur > 0 else 0.1
    dst.parent.mkdir(parents=True, exist_ok=True)
    # Nudge past the first video PTS so input-seek does not snap *before* it
    # and keep the empty lead-in.
    seek = lead + 0.02 if lead > 0 else 0.0
    cmd: list[str] = ["ffmpeg", "-y"]
    if seek > 0:
        cmd += ["-ss", f"{seek:.3f}"]
    cmd += [
        "-i",
        str(src),
        "-t",
        f"{keep:.3f}",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        str(dst),
    ]
    run(cmd)


def _x264_superfast_args(output: Path) -> list[str]:
    return [
        "-c:v",
        "libx264",
        "-preset",
        "superfast",
        "-crf",
        "20",
        "-threads",
        "0",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        str(output),
    ]


def stitch_one(
    clips: list[Path],
    output: Path,
    *,
    reencode: bool,
    width: int | None = None,
    height: int | None = None,
    fps: float | None = None,
    trim_lead: float = 0.0,
    trim_trail: float = 0.0,
    detect_freeze: bool = True,
) -> dict[str, Any]:
    """
    Concatenate clips. Default is stream-copy (no encoder): freeze-detect,
    ``-c copy`` trim each gameplay clip, concat demuxer copy. Lobby is a PNG
    sidecar only. ``reencode=True`` uses filter_complex concat.

    ``detect_freeze`` (default) drops frozen open/close frames per gameplay
    clip. ``trim_lead`` / ``trim_trail`` are a fixed fallback when detection
    is off. Lobby intro is never trimmed.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    lead = max(0.0, float(trim_lead))
    trail = max(0.0, float(trim_trail))
    freeze_trims: list[dict[str, Any]] = []
    trim_cache: dict[str, tuple[float, float]] = {}

    def clip_trims(clip: Path) -> tuple[float, float]:
        key = str(clip)
        if key in trim_cache:
            return trim_cache[key]
        is_lobby = "lobby" in clip.name.lower()
        if is_lobby:
            trim_cache[key] = (0.0, 0.0)
            return 0.0, 0.0
        if detect_freeze:
            dur = float(probe_video(clip).get("duration") or 0.0)
            fl, ft = detect_edge_freezes(clip, duration=dur)
            freeze_trims.append(
                {"clip": clip.name, "lead": fl, "trail": ft, "duration": round(dur, 3)}
            )
            if fl > 0 or ft > 0:
                print(
                    f"[freeze] {clip.name}  cut start {fl:.2f}s  end {ft:.2f}s",
                    flush=True,
                )
            trim_cache[key] = (fl, ft)
            return fl, ft
        trim_cache[key] = (lead, trail)
        return lead, trail

    def filter_concat(src_clips: list[Path], *, already_trimmed: bool = False) -> None:
        probe = probe_video(src_clips[0])
        w = int(width or probe["width"])
        h = int(height or probe["height"])
        rate = float(fps or probe["fps"])
        w -= w % 2
        h -= h % 2
        rate = max(1.0, min(rate, 60.0))
        cmd: list[str] = ["ffmpeg", "-y"]
        for clip in src_clips:
            cmd.extend(["-i", str(clip)])
        filter_parts: list[str] = []
        for i, clip in enumerate(src_clips):
            clip_lead, clip_trail = (0.0, 0.0) if already_trimmed else clip_trims(clip)
            if clip_lead > 0 or clip_trail > 0:
                dur = float(probe_video(clip).get("duration") or 0.0)
                end = dur - clip_trail if (clip_trail > 0 and dur > 0) else 0.0
                if end > clip_lead + 0.1:
                    v_head = (
                        f"trim=start={clip_lead:.3f}:end={end:.3f},"
                        f"setpts=PTS-STARTPTS,"
                    )
                    a_head = (
                        f"atrim=start={clip_lead:.3f}:end={end:.3f},"
                        f"asetpts=PTS-STARTPTS,"
                    )
                elif clip_lead > 0:
                    v_head = f"trim=start={clip_lead:.3f},setpts=PTS-STARTPTS,"
                    a_head = f"atrim=start={clip_lead:.3f},asetpts=PTS-STARTPTS,"
                else:
                    v_head = "setpts=PTS-STARTPTS,"
                    a_head = "asetpts=PTS-STARTPTS,"
            else:
                v_head = "setpts=PTS-STARTPTS,"
                a_head = "asetpts=PTS-STARTPTS,"
            filter_parts.append(
                f"[{i}:v]{v_head}"
                f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x0A0C12,"
                f"fps={rate:.3f},setsar=1[v{i}];"
                f"[{i}:a]{a_head}"
                f"aformat=sample_rates=48000:channel_layouts=stereo[a{i}];"
            )
        concat_in = "".join(f"[v{i}][a{i}]" for i in range(len(src_clips)))
        filter_complex = (
            "".join(filter_parts)
            + f"{concat_in}concat=n={len(src_clips)}:v=1:a=1[v][a]"
        )
        cmd.extend(
            [
                "-filter_complex",
                filter_complex,
                "-map",
                "[v]",
                "-map",
                "[a]",
                *_x264_superfast_args(output),
            ]
        )
        run(cmd)

    def concat_copy(src_clips: list[Path]) -> None:
        list_path = output.with_suffix(".concat.txt")
        lines = []
        for clip in src_clips:
            escaped = str(clip.resolve()).replace("'", r"'\''")
            lines.append(f"file '{escaped}'")
        list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            run(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(list_path),
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(output),
                ]
            )
        finally:
            list_path.unlink(missing_ok=True)

    if reencode:
        filter_concat(clips)
        mode = "filter_concat"
        if detect_freeze:
            mode = "filter_concat_freeze_detect"
        elif lead > 0 or trail > 0:
            mode = f"filter_concat_trim_lead_{lead:.2f}s_trail_{trail:.2f}s"
    else:
        with tempfile.TemporaryDirectory(prefix="stitch_trim_") as tmp:
            tmp_dir = Path(tmp)
            work: list[Path] = []
            for i, clip in enumerate(clips):
                clip_lead, clip_trail = clip_trims(clip)
                if clip_lead <= 0 and clip_trail <= 0:
                    work.append(clip)
                    continue
                dur = float(probe_video(clip).get("duration") or 0.0)
                dest = tmp_dir / f"{i:02d}_{clip.name}"
                copy_trim_clip(clip, dest, clip_lead, clip_trail, dur)
                work.append(dest)
            concat_copy(work)
            mode = "copy_trim" if detect_freeze else "copy"

    result = {
        "output": str(output),
        "filename": output.name,
        "clipCount": len(clips),
        "sources": [c.name for c in clips],
        "bytes": output.stat().st_size if output.is_file() else 0,
        "mode": mode,
    }
    if freeze_trims:
        result["freezeTrims"] = freeze_trims
    return result


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Stitch lol_clips/gNN_Champ/*.mp4 into one weave per game."
    )
    p.add_argument("--dataset-dir", type=Path, required=True)
    p.add_argument(
        "--clips-dir",
        type=Path,
        default=None,
        help="Default: <dataset>/lol_clips",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Default: <dataset>/lol_compilations",
    )
    p.add_argument(
        "--reencode",
        action="store_true",
        help="Always re-encode via filter concat (slower). Default is copy-trim + stream copy.",
    )
    p.add_argument(
        "--min-clips",
        type=int,
        default=2,
        help="Skip games with fewer than N clips after ranking (default: 2)",
    )
    p.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Rank clips and stitch only the best N per game (default: 5). 0 = all clips.",
    )
    p.add_argument(
        "--no-rank",
        action="store_true",
        help="Skip ranking; stitch every clip in each game folder.",
    )
    p.add_argument(
        "--daily",
        action="store_true",
        help="One compilation from the top clips across every game (not per-game weaves).",
    )
    p.add_argument(
        "--max-per-game",
        type=int,
        default=3,
        help="With --daily, max clips from one game (default: 3). 0 = no cap.",
    )
    p.add_argument(
        "--order",
        choices=("chrono", "score"),
        default="chrono",
        help="With --daily, clip order (default: chrono).",
    )
    p.add_argument("--force", action="store_true", help="Overwrite existing weaves")
    p.add_argument(
        "--lobby-seconds",
        type=float,
        default=3.0,
        help="Lobby card intro length in seconds (default: 3). Use 0 to disable.",
    )
    p.add_argument(
        "--no-lobby",
        action="store_true",
        help="Skip lobby card intro even if --lobby-seconds > 0",
    )
    p.add_argument(
        "--detect-freeze",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Detect and drop frozen frames at the start/end of each gameplay "
        "clip (default: on). Lobby intro is never trimmed.",
    )
    p.add_argument(
        "--trim-lead",
        type=float,
        default=PAD_LEAD_S,
        help=(
            f"Fixed seconds to drop from the start of each gameplay clip when "
            f"--no-detect-freeze (default: {PAD_LEAD_S}). Must match cut "
            f"--pad-lead. Use 0 to disable."
        ),
    )
    p.add_argument(
        "--trim-trail",
        type=float,
        default=PAD_TRAIL_S,
        help=(
            f"Fixed seconds to drop from the end of each gameplay clip when "
            f"--no-detect-freeze (default: {PAD_TRAIL_S}). Must match cut "
            f"--pad-trail. Use 0 to disable."
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dataset_dir = args.dataset_dir.resolve()
    clips_dir = (args.clips_dir or dataset_dir / "lol_clips").resolve()
    out_dir = (args.output_dir or dataset_dir / "lol_compilations").resolve()
    want_lobby = (not args.no_lobby) and float(args.lobby_seconds) > 0

    if not clips_dir.is_dir():
        print(f"error: missing clips dir {clips_dir}", file=sys.stderr)
        return 1

    if args.daily:
        k = int(args.top_k) if int(args.top_k) > 0 else 12
        if args.no_rank:
            print("error: --daily requires ranking (omit --no-rank)", file=sys.stderr)
            return 2
        out_dir.mkdir(parents=True, exist_ok=True)
        paths, rank_meta = apply_daily_top_k(
            dataset_dir=dataset_dir,
            clips_dir=clips_dir,
            top_k=k,
            max_per_game=int(args.max_per_game),
            order=str(args.order),
            extra_dirs=[out_dir],
        )
        if len(paths) < max(1, int(args.min_clips)):
            print(f"error: daily slate has {len(paths)} clip(s)", file=sys.stderr)
            return 1
        output = out_dir / f"daily_top{k}.mp4"
        if output.exists() and not args.force:
            print(f"[skip] exists {output.name} (pass --force)", file=sys.stderr)
            return 0
        probe = probe_video(paths[0])
        print(f"[stitch] daily × {len(paths)} → {output.name}", flush=True)
        info = stitch_one(
            paths,
            output,
            reencode=bool(args.reencode),
            width=int(probe["width"]),
            height=int(probe["height"]),
            fps=float(probe["fps"]),
            trim_lead=float(args.trim_lead),
            trim_trail=float(args.trim_trail),
            detect_freeze=bool(args.detect_freeze),
        )
        report = {
            "schema_version": 2,
            "dataset_id": dataset_dir.name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "scope": "daily",
            "rank": rank_meta,
            "output": str(output),
            "filename": output.name,
            "clipCount": len(paths),
            "sources": [p.name for p in paths],
            "mode": info.get("mode"),
        }
        (out_dir / "compilations.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({"status": "ok", **{k: report[k] for k in ("filename", "clipCount", "scope")}}, indent=2))
        return 0

    manifest_path = clips_dir / "clips.json"
    manifest = load_json(manifest_path) if manifest_path.is_file() else None
    if manifest is not None:
        games = games_from_manifest(clips_dir, manifest)
    else:
        games = discover_games(clips_dir)

    rank_meta: dict[str, Any] = {"top_k": 0, "enabled": False}
    top_k = 0 if args.no_rank else int(args.top_k)
    if top_k > 0:
        games, rank_meta = apply_top_k(
            games,
            dataset_dir=dataset_dir,
            clips_dir=clips_dir,
            top_k=top_k,
            extra_dirs=[out_dir],
        )
        rank_meta["enabled"] = True

    if want_lobby and not os.environ.get("RIOT_API_KEY", "").strip():
        print(
            "warning: RIOT_API_KEY missing — stitching without lobby intros",
            flush=True,
        )
        want_lobby = False

    out_dir.mkdir(parents=True, exist_ok=True)
    weaves: list[dict[str, Any]] = []

    for folder, clips in games:
        if len(clips) < args.min_clips:
            print(f"[skip] {folder}: only {len(clips)} clip(s)", flush=True)
            continue

        # Prefer clipIndex / gameIndex from manifest for stable gamNN naming.
        game_index = 1
        if manifest:
            for item in manifest.get("clips") or []:
                rel = str(item.get("relativePath") or "")
                if rel.startswith(f"{folder}/") and item.get("gameIndex"):
                    game_index = int(item["gameIndex"])
                    break
        else:
            # g01_Leblanc_vsX → 1
            try:
                game_index = int(folder.split("_", 1)[0].lstrip("g"))
            except ValueError:
                game_index = 1

        meta = match_meta_for_folder(folder, dataset_dir, manifest)
        weave_name = weave_output_name(game_index, meta)
        output = out_dir / weave_name
        if output.exists() and not args.force:
            print(f"[skip] exists {output.name} (pass --force)", flush=True)
            weaves.append(
                {
                    "gameFolder": folder,
                    "output": str(output),
                    "filename": output.name,
                    "clipCount": len(clips),
                    "skipped": True,
                    "laneOpponentChampion": meta.get("laneOpponentChampion"),
                    "win": meta.get("win"),
                }
            )
            continue

        stitch_clips = list(clips)
        lobby_meta: dict[str, Any] = {"included": False}
        force_reencode = bool(args.reencode)
        probe = probe_video(clips[0])

        if want_lobby:
            match_id = meta.get("matchId") or match_id_for_folder(folder, dataset_dir, manifest)
            if not match_id:
                print(f"[lobby] skip {folder}: no matchId mapping", flush=True)
            else:
                try:
                    lobby_png = out_dir / f"{Path(weave_name).stem}_lobby.png"
                    print(f"[lobby] {folder} match={match_id}", flush=True)
                    generate_lobby_png(match_id, lobby_png)
                    # PNG + meta only — no lobby mp4. Portrait burns the overlay intro.
                    stitch_clips = list(clips)
                    lobby_meta = {
                        "included": True,
                        "matchId": match_id,
                        "seconds": float(args.lobby_seconds),
                        "png": lobby_png.name,
                    }
                except Exception as exc:
                    print(f"[lobby] failed {folder}: {exc}", flush=True)
                    lobby_meta = {"included": False, "error": str(exc)}

        print(f"[stitch] {folder} × {len(stitch_clips)} → {output.name}", flush=True)
        info = stitch_one(
            stitch_clips,
            output,
            reencode=force_reencode,
            width=int(probe["width"]),
            height=int(probe["height"]),
            fps=float(probe["fps"]),
            trim_lead=float(args.trim_lead),
            trim_trail=float(args.trim_trail),
            detect_freeze=bool(args.detect_freeze),
        )
        info["gameFolder"] = folder
        info["filename"] = output.name
        info["laneOpponentChampion"] = meta.get("laneOpponentChampion")
        info["win"] = meta.get("win")
        info["lobby"] = lobby_meta
        weaves.append(info)

    report = {
        "schema_version": 2,
        "dataset_id": dataset_dir.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "clips_dir": str(clips_dir),
        "output_dir": str(out_dir),
        "lobby_seconds": float(args.lobby_seconds) if want_lobby else 0,
        "trim_lead": float(args.trim_lead),
        "trim_trail": float(args.trim_trail),
        "detect_freeze": bool(args.detect_freeze),
        "rank": rank_meta,
        "game_count": len(weaves),
        "weaves": weaves,
    }
    report_path = out_dir / "compilations.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "ok",
                "output_dir": str(out_dir),
                "game_count": len(weaves),
                "lobby_seconds": report["lobby_seconds"],
                "top_k": rank_meta.get("top_k") or 0,
                "ranked_clips": rank_meta.get("files") or [],
                "weaves": [w.get("filename") for w in weaves],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
