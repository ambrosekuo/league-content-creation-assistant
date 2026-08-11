#!/usr/bin/env python3
"""Weave per-game LoL clips into one compilation video per game folder.

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
            if f"g{i:02d}_{champ}" == folder:
                mid = m.get("matchId")
                return str(mid) if mid else None
    return None


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
            "-of",
            "json",
            str(path),
        ],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        return {"width": 1920, "height": 1080, "fps": 30.0}
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
    return {
        "width": int(s0.get("width") or 1920),
        "height": int(s0.get("height") or 1080),
        "fps": max(1.0, min(fps, 60.0)),
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
            "-tune",
            "stillimage",
            "-pix_fmt",
            "yuv420p",
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
    run(
        [
            sys.executable,
            str(ROOT / "generate_lobby_card.py"),
            "--match-id",
            match_id,
            "--output",
            str(output),
        ]
    )
    return output


def stitch_one(
    clips: list[Path],
    output: Path,
    *,
    reencode: bool,
    width: int | None = None,
    height: int | None = None,
    fps: float | None = None,
) -> dict[str, Any]:
    """
    Concatenate clips. When reencode=True (required for lobby intros), use
    filter_complex concat so video/audio stay aligned — concat demuxer often
    leaves the still lobby on screen while the next clip's audio starts.
    """
    output.parent.mkdir(parents=True, exist_ok=True)

    if reencode:
        probe = probe_video(clips[0])
        w = int(width or probe["width"])
        h = int(height or probe["height"])
        rate = float(fps or probe["fps"])
        w -= w % 2
        h -= h % 2
        rate = max(1.0, min(rate, 60.0))

        cmd: list[str] = ["ffmpeg", "-y"]
        for clip in clips:
            cmd.extend(["-i", str(clip)])

        filter_parts: list[str] = []
        for i in range(len(clips)):
            filter_parts.append(
                f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x0A0C12,"
                f"fps={rate:.3f},setsar=1,setpts=PTS-STARTPTS[v{i}];"
                f"[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                f"asetpts=PTS-STARTPTS[a{i}];"
            )
        concat_in = "".join(f"[v{i}][a{i}]" for i in range(len(clips)))
        filter_complex = (
            "".join(filter_parts)
            + f"{concat_in}concat=n={len(clips)}:v=1:a=1[v][a]"
        )
        cmd.extend(
            [
                "-filter_complex",
                filter_complex,
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-movflags",
                "+faststart",
                str(output),
            ]
        )
        run(cmd)
        mode = "filter_concat"
    else:
        list_path = output.with_suffix(".concat.txt")
        lines = []
        for clip in clips:
            escaped = str(clip.resolve()).replace("'", r"'\''")
            lines.append(f"file '{escaped}'")
        list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
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
                mode = "copy"
            except RuntimeError:
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
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-crf",
                        "20",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "160k",
                        "-movflags",
                        "+faststart",
                        str(output),
                    ]
                )
                mode = "reencode_fallback"
        finally:
            list_path.unlink(missing_ok=True)

    return {
        "output": str(output),
        "filename": output.name,
        "clipCount": len(clips),
        "sources": [c.name for c in clips],
        "bytes": output.stat().st_size if output.is_file() else 0,
        "mode": mode,
    }


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
        help="Always re-encode (slower). Default tries stream copy first.",
    )
    p.add_argument(
        "--min-clips",
        type=int,
        default=2,
        help="Skip games with fewer than N clips (default: 2)",
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

    manifest_path = clips_dir / "clips.json"
    manifest = load_json(manifest_path) if manifest_path.is_file() else None
    if manifest is not None:
        games = games_from_manifest(clips_dir, manifest)
    else:
        games = discover_games(clips_dir)

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
        output = out_dir / f"{folder}_weave.mp4"
        if output.exists() and not args.force:
            print(f"[skip] exists {output.name} (pass --force)", flush=True)
            weaves.append(
                {
                    "gameFolder": folder,
                    "output": str(output),
                    "filename": output.name,
                    "clipCount": len(clips),
                    "skipped": True,
                }
            )
            continue

        stitch_clips = list(clips)
        lobby_meta: dict[str, Any] = {"included": False}
        force_reencode = bool(args.reencode)
        probe = probe_video(clips[0])

        if want_lobby:
            match_id = match_id_for_folder(folder, dataset_dir, manifest)
            if not match_id:
                print(f"[lobby] skip {folder}: no matchId mapping", flush=True)
            else:
                try:
                    lobby_png = out_dir / f"{folder}_lobby.png"
                    print(f"[lobby] {folder} match={match_id}", flush=True)
                    generate_lobby_png(match_id, lobby_png)
                    with tempfile.TemporaryDirectory(prefix=f"lobby_{folder}_") as tmp:
                        lobby_mp4 = Path(tmp) / "00_lobby.mp4"
                        still_to_video(
                            lobby_png,
                            lobby_mp4,
                            seconds=float(args.lobby_seconds),
                            width=int(probe["width"]),
                            height=int(probe["height"]),
                            fps=float(probe["fps"]),
                        )
                        # Keep a durable copy next to the weave for debugging
                        durable = out_dir / f"{folder}_lobby_intro.mp4"
                        durable.write_bytes(lobby_mp4.read_bytes())
                        stitch_clips = [durable] + stitch_clips
                    lobby_meta = {
                        "included": True,
                        "matchId": match_id,
                        "seconds": float(args.lobby_seconds),
                        "png": lobby_png.name,
                        "intro": f"{folder}_lobby_intro.mp4",
                    }
                    force_reencode = True  # lobby + gameplay need synced filter concat
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
        )
        info["gameFolder"] = folder
        info["lobby"] = lobby_meta
        weaves.append(info)

    report = {
        "schema_version": 2,
        "dataset_id": dataset_dir.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "clips_dir": str(clips_dir),
        "output_dir": str(out_dir),
        "lobby_seconds": float(args.lobby_seconds) if want_lobby else 0,
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
                "weaves": [w.get("filename") for w in weaves],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
