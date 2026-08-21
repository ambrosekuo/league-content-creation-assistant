#!/usr/bin/env python3

from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image

from ffmpeg_color import VIDEO_TO_BT709, X264_BT709
from render_rank_cards import (
    OVERLAY_STREAMERS_CY,
    OVERLAY_STREAMERS_TILE,
    detect_streamers,
    draw_streamer_callouts,
    load_json,
)


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, text=True, capture_output=True)

    if proc.returncode != 0:
        raise RuntimeError(
            f"Command failed:\n{' '.join(cmd)}\n\n"
            f"{proc.stderr or proc.stdout}"
        )


def norm(text: str) -> str:
    return "".join(
        ch.lower()
        for ch in str(text or "")
        if ch.isalnum()
    )


def first_keyframe_at_or_after(path: Path, seconds: float) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-skip_frame",
            "nokey",
            "-show_entries",
            "frame=best_effort_timestamp_time",
            "-of",
            "csv=p=0",
            str(path),
        ],
        text=True,
        capture_output=True,
    )

    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or "ffprobe failed")

    last = None

    for line in proc.stdout.splitlines():
        raw = line.strip().strip(",")

        try:
            t = float(raw)
        except ValueError:
            continue

        last = t

        if t >= seconds:
            return t

    raise RuntimeError(
        f"No keyframe found after {seconds:.3f}s "
        f"(last={last})"
    )


def player_is_ally(player: dict[str, Any], meta: dict[str, Any]) -> bool:
    me = meta.get("me") or {}

    if (
        player.get("teamId") is not None
        and me.get("teamId") is not None
    ):
        return int(player["teamId"]) == int(me["teamId"])

    return bool(player.get("win")) == bool(me.get("win", True))


def find_player(
    meta: dict[str, Any],
    query: str,
) -> dict[str, Any] | None:

    wanted = norm(query)

    for player in meta.get("players") or []:
        if player.get("mine"):
            continue

        candidates = [
            player.get("name"),
            f"{player.get('name', '')}{player.get('tag', '')}",
        ]

        if any(norm(x) == wanted for x in candidates):
            return player

    # Allow partial match as fallback.
    matches = []

    for player in meta.get("players") or []:
        if player.get("mine"):
            continue

        name = norm(player.get("name"))

        if wanted and (
            wanted in name
            or name in wanted
        ):
            matches.append(player)

    if len(matches) == 1:
        return matches[0]

    return None


def manual_streamer(
    meta: dict[str, Any],
    name: str,
) -> dict[str, Any]:

    player = find_player(meta, name)

    if player is None:
        raise RuntimeError(
            f"Could not find included player {name!r} "
            "in lobby metadata."
        )

    return {
        "id": norm(player.get("name") or name),
        "display": str(player.get("name") or name),
        "name": str(player.get("name") or name),
        "tag": str(player.get("tag") or ""),
        "champion": str(player.get("champion") or ""),
        "ally": player_is_ally(player, meta),
        "position": str(player.get("position") or ""),
        "kind": "streamer",
        "source": "manual",
        "org": "",
        "login": None,
        "broadcasterType": "",
    }


def apply_overrides(
    meta: dict[str, Any],
    detected: list[dict[str, Any]],
    *,
    includes: list[str],
    excludes: list[str],
) -> list[dict[str, Any]]:

    excluded = {norm(x) for x in excludes}

    output = []

    for row in detected:
        keys = {
            norm(row.get("id")),
            norm(row.get("display")),
            norm(row.get("name")),
            norm(row.get("login")),
        }

        if keys & excluded:
            print(
                f"[override] exclude "
                f"{row.get('display') or row.get('name')}",
                flush=True,
            )
            continue

        output.append(row)

    existing = set()

    for row in output:
        existing.update(
            {
                norm(row.get("id")),
                norm(row.get("display")),
                norm(row.get("name")),
            }
        )

    for name in includes:
        key = norm(name)

        if key in excluded:
            print(
                f"[override] {name!r} is both include/exclude; "
                "exclude wins",
                flush=True,
            )
            continue

        if key in existing:
            continue

        row = manual_streamer(meta, name)

        output.append(row)

        existing.update(
            {
                norm(row.get("id")),
                norm(row.get("display")),
                norm(row.get("name")),
            }
        )

        print(
            f"[override] include {row['display']} "
            f"({row['champion']}, "
            f"{'ally' if row['ally'] else 'enemy'})",
            flush=True,
        )

    return output


def main() -> int:
    p = argparse.ArgumentParser(
        description=(
            "Patch streamer callouts onto the beginning of an "
            "existing portrait without re-encoding the whole video."
        )
    )

    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--meta", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)

    p.add_argument(
        "--include",
        action="append",
        default=[],
        help=(
            "Force-include a lobby player by Riot name. "
            "Champion/team are read from lobby metadata."
        ),
    )

    p.add_argument(
        "--exclude",
        action="append",
        default=[],
        help=(
            "Remove an automatically detected notable. "
            "May be supplied multiple times."
        ),
    )

    p.add_argument(
        "--no-auto",
        action="store_true",
        help="Disable automatic streamer/pro detection.",
    )

    p.add_argument(
        "--hold",
        type=float,
        default=2.0,
        help="Seconds streamer callouts remain visible.",
    )

    p.add_argument(
        "--patch-seconds",
        type=float,
        default=3.0,
        help=(
            "Minimum opening duration to encode. "
            "Actual join snaps to the next source keyframe."
        ),
    )

    p.add_argument("--crf", type=int, default=20)
    p.add_argument("--preset", default="veryfast")

    args = p.parse_args()

    source = args.input.resolve()
    meta_path = args.meta.resolve()
    output = args.output.resolve()

    if not source.is_file():
        raise FileNotFoundError(source)

    if not meta_path.is_file():
        raise FileNotFoundError(meta_path)

    if source == output:
        raise SystemExit(
            "Use a separate --output while testing."
        )

    meta = load_json(meta_path)

    if args.no_auto:
        detected = []
    else:
        detected = detect_streamers(meta)

    streamers = apply_overrides(
        meta,
        detected,
        includes=list(args.include),
        excludes=list(args.exclude),
    )

    if not streamers:
        raise RuntimeError(
            "No streamer callouts remain after detection/overrides."
        )

    print("[overlay] final callouts:", flush=True)

    for row in streamers:
        print(
            "  "
            f"{row.get('display')} | "
            f"{row.get('champion')} | "
            f"{'ally' if row.get('ally') else 'enemy'} | "
            f"{row.get('kind')} | "
            f"{row.get('source')}",
            flush=True,
        )

    hold = max(0.2, float(args.hold))

    requested_patch = max(
        float(args.patch_seconds),
        hold + 0.5,
    )

    cut_time = first_keyframe_at_or_after(
        source,
        requested_patch,
    )

    print(
        f"[patch] encode 0-{cut_time:.3f}s; "
        f"stream-copy remainder",
        flush=True,
    )

    with tempfile.TemporaryDirectory(
        prefix="streamer_overlay_"
    ) as raw_tmp:

        tmp = Path(raw_tmp)

        overlay_png = tmp / "streamers.png"
        head = tmp / "head.mp4"
        tail = tmp / "tail.mp4"
        concat_txt = tmp / "concat.txt"

        overlay = Image.new(
            "RGBA",
            (1080, 1920),
            (0, 0, 0, 0),
        )

        draw_streamer_callouts(
            overlay,
            streamers,
            cy=OVERLAY_STREAMERS_CY,
            tile=OVERLAY_STREAMERS_TILE,
        )

        overlay.save(overlay_png)

        fade_in = 0.10
        fade_out = 0.35

        fade_out_at = max(
            0.0,
            hold - fade_out,
        )

        filt = (
            "[1:v]"
            "format=rgba,"
            f"fade=t=in:st=0:d={fade_in:.3f}:alpha=1,"
            f"fade=t=out:st={fade_out_at:.3f}:"
            f"d={fade_out:.3f}:alpha=1[ov];"
            "[0:v][ov]"
            "overlay=0:0:format=auto:eof_action=pass,"
            f"{VIDEO_TO_BT709}[v]"
        )

        # Re-encode opening only.
        run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(source),
                "-loop",
                "1",
                "-t",
                f"{hold + 0.10:.3f}",
                "-i",
                str(overlay_png),
                "-filter_complex",
                filt,
                "-map",
                "[v]",
                "-map",
                "0:a?",
                "-t",
                f"{cut_time:.3f}",
                "-c:v",
                "libx264",
                "-preset",
                args.preset,
                "-crf",
                str(args.crf),
                *X264_BT709,
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-movflags",
                "+faststart",
                str(head),
            ]
        )

        # Copy untouched remainder.
        run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(source),
                "-ss",
                f"{cut_time:.3f}",
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-c",
                "copy",
                "-avoid_negative_ts",
                "make_zero",
                str(tail),
            ]
        )

        concat_txt.write_text(
            f"file '{head.as_posix()}'\n"
            f"file '{tail.as_posix()}'\n",
            encoding="utf-8",
        )

        output.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_txt),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(output),
            ]
        )

    print(f"[done] {output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())