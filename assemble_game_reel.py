#!/usr/bin/env python3
"""
Assemble a per-game highlight reel locally:

  [5s lobby card] + [optional 5s game start] + [KDA clips] + [nexus/end]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from env_loader import load_dotenv


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip() or "command failed")


def find_source(dataset_dir: Path) -> Path | None:
    for name in ("source.mp4", "source.mkv", "source.webm"):
        p = dataset_dir / name
        if p.is_file() and p.stat().st_size > 1_000_000:
            return p
    return None


def still_to_video(image: Path, output: Path, *, seconds: float = 5.0) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            str(image),
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-t",
            f"{seconds:.2f}",
            "-c:v",
            "libx264",
            "-tune",
            "stillimage",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    return output


def cut_segment(
    source: Path,
    output: Path,
    start: float,
    duration: float,
) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    start = max(0.0, start)
    duration = max(0.1, duration)
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    return output


def concat_copy(clips: list[Path], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
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
                    "18",
                    "-c:a",
                    "aac",
                    "-movflags",
                    "+faststart",
                    str(output),
                ]
            )
    finally:
        list_path.unlink(missing_ok=True)
    return output


def match_bookends(match: dict[str, Any]) -> tuple[float | None, float | None, bool]:
    """Return (game_start_vod_offset, game_end_vod_offset, win)."""
    start_off: float | None = None
    end_off: float | None = None
    for event in match.get("events") or []:
        if event.get("type") == "GAME_START" and event.get("vodOffsetSeconds") is not None:
            start_off = float(event["vodOffsetSeconds"])
        if event.get("type") == "GAME_END" and event.get("vodOffsetSeconds") is not None:
            end_off = float(event["vodOffsetSeconds"])
    # Fallback: derive from first/last timed event + duration
    if start_off is None or end_off is None:
        timed = [
            float(e["vodOffsetSeconds"])
            for e in (match.get("events") or [])
            if e.get("vodOffsetSeconds") is not None
        ]
        if timed and start_off is None:
            # Approximate start from earliest event minus its gameTime
            for e in match.get("events") or []:
                if e.get("vodOffsetSeconds") is None or e.get("gameTimeMs") is None:
                    continue
                start_off = float(e["vodOffsetSeconds"]) - float(e["gameTimeMs"]) / 1000.0
                break
        if timed and end_off is None:
            dur = float(match.get("gameDurationSeconds") or 0)
            if start_off is not None and dur > 0:
                end_off = start_off + dur
            else:
                end_off = max(timed)
    return start_off, end_off, bool(match.get("win"))


def game_folder_name(game_index: int, champion: str) -> str:
    champ = "".join(ch for ch in champion if ch.isalnum()) or "Unknown"
    return f"g{game_index:02d}_{champ}"


def assemble_one(
    *,
    dataset_dir: Path,
    match: dict[str, Any],
    game_index: int,
    source: Path | None,
    out_dir: Path,
    lobby_seconds: float,
    start_seconds: float,
    end_seconds: float,
    end_tail: float,
    clips_folder: str | None = None,
    lobby_png_in: Path | None = None,
    start_clip: Path | None = None,
    end_clip: Path | None = None,
) -> dict[str, Any]:
    champ = str(match.get("champion") or "Unknown")
    match_id = str(match.get("matchId"))
    folder = clips_folder or game_folder_name(game_index, champ)
    work = out_dir / f"_work_{folder}"
    work.mkdir(parents=True, exist_ok=True)

    # 1) Lobby card
    lobby_png = out_dir / f"{folder}_lobby.png"
    if lobby_png_in is not None and lobby_png_in.is_file():
        if lobby_png_in.resolve() != lobby_png.resolve():
            lobby_png.write_bytes(lobby_png_in.read_bytes())
    else:
        run(
            [
                sys.executable,
                str(Path(__file__).resolve().parent / "generate_lobby_card.py"),
                "--match-id",
                match_id,
                "--output",
                str(lobby_png),
            ]
        )
    parts: list[Path] = []
    lobby_mp4 = work / "00_lobby.mp4"
    still_to_video(lobby_png, lobby_mp4, seconds=lobby_seconds)
    parts.append(lobby_mp4)

    start_off, end_off, _win = match_bookends(match)

    # 2) Optional game start (skip if before VOD / missing media)
    start_used = False
    if start_clip is not None and start_clip.is_file():
        start_mp4 = work / "01_start.mp4"
        start_mp4.write_bytes(start_clip.read_bytes())
        parts.append(start_mp4)
        start_used = True
    elif source is not None and start_off is not None and start_off >= -0.5:
        start_mp4 = work / "01_start.mp4"
        cut_segment(source, start_mp4, max(0.0, start_off), start_seconds)
        parts.append(start_mp4)
        start_used = True

    # 3) Existing KDA clips in game folder (ordered)
    clips_dir = dataset_dir / "lol_clips" / folder
    kda_clips = sorted(clips_dir.glob("c*.mp4")) if clips_dir.is_dir() else []
    parts.extend(kda_clips)

    # 4) Always try to include nexus / GAME_END tail
    end_used = False
    if end_clip is not None and end_clip.is_file():
        end_mp4 = work / "99_end.mp4"
        end_mp4.write_bytes(end_clip.read_bytes())
        parts.append(end_mp4)
        end_used = True
    elif source is not None and end_off is not None:
        # Window covering final push + nexus + victory/defeat banner.
        end_start = max(0.0, end_off - end_seconds)
        end_dur = end_seconds + max(0.0, end_tail)
        end_mp4 = work / "99_end.mp4"
        cut_segment(source, end_mp4, end_start, end_dur)
        parts.append(end_mp4)
        end_used = True

    if len(parts) < 2:
        raise RuntimeError(f"Not enough segments to assemble {folder}")

    final = out_dir / f"{folder}_reel.mp4"
    concat_copy(parts, final)
    return {
        "matchId": match_id,
        "champion": champ,
        "gameIndex": game_index,
        "output": str(final),
        "lobbyCard": str(lobby_png),
        "startIncluded": start_used,
        "endIncluded": end_used,
        "kdaClips": len(kda_clips),
        "segments": [p.name for p in parts],
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Assemble lobby+start+clips+end game reels.")
    p.add_argument("--dataset-dir", type=Path, required=True)
    p.add_argument("--match-id", default=None, help="Only assemble this match")
    p.add_argument("--output-dir", type=Path, default=None)
    p.add_argument(
        "--clips-folder",
        default=None,
        help="Override lol_clips/<folder> (e.g. g02_Ahri when assembling one match)",
    )
    p.add_argument("--lobby-png", type=Path, default=None, help="Reuse an existing lobby card PNG")
    p.add_argument(
        "--start-clip",
        type=Path,
        default=None,
        help="Pre-cut game-start mp4 (skips cutting from source)",
    )
    p.add_argument(
        "--end-clip",
        type=Path,
        default=None,
        help="Pre-cut nexus/end mp4 (skips cutting from source)",
    )
    p.add_argument("--lobby-seconds", type=float, default=5.0)
    p.add_argument("--start-seconds", type=float, default=5.0, help="Load-in / game start length")
    p.add_argument(
        "--end-seconds",
        type=float,
        default=15.0,
        help="Seconds before GAME_END to include (nexus push)",
    )
    p.add_argument(
        "--end-tail",
        type=float,
        default=4.0,
        help="Seconds after GAME_END for victory/defeat banner",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    dataset_dir = args.dataset_dir.resolve()
    events_path = dataset_dir / "lol_events.json"
    if not events_path.is_file():
        print(f"error: missing {events_path}", file=sys.stderr)
        return 1

    payload = load_json(events_path)
    all_matches = list(payload.get("matches") or [])
    matches = list(all_matches)
    if args.match_id:
        matches = [m for m in matches if m.get("matchId") == args.match_id]
    if not matches:
        print("error: no matches to assemble", file=sys.stderr)
        return 1

    source = find_source(dataset_dir)
    if source is None and not (args.start_clip or args.end_clip):
        print(
            "warning: no source.* in dataset — will build lobby+clips only "
            "(no start/end bookends). Pass --start-clip/--end-clip or download source.mp4.",
            flush=True,
        )

    out_dir = (args.output_dir or dataset_dir / "lol_compilations").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Prefer chronological order already in lol_events
    results = []
    for match in matches:
        # Preserve original game index when filtering to one match
        idx = next(
            (i for i, m in enumerate(all_matches, start=1) if m.get("matchId") == match.get("matchId")),
            1,
        )
        print(f"[assemble] g{idx:02d} {match.get('champion')} {match.get('matchId')}", flush=True)
        results.append(
            assemble_one(
                dataset_dir=dataset_dir,
                match=match,
                game_index=idx,
                source=source,
                out_dir=out_dir,
                lobby_seconds=args.lobby_seconds,
                start_seconds=args.start_seconds,
                end_seconds=args.end_seconds,
                end_tail=args.end_tail,
                clips_folder=args.clips_folder,
                lobby_png_in=args.lobby_png,
                start_clip=args.start_clip,
                end_clip=args.end_clip,
            )
        )

    report = {"status": "ok", "reels": results}
    (out_dir / "reels.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
