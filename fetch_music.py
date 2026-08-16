#!/usr/bin/env python3
"""Royalty-free lofi / chill beds from Mixkit (YouTube-safe, no attribution).

  python fetch_music.py                 # status
  python fetch_music.py --suggest       # download catalog → assets/music/suggested/
  python fetch_music.py --preview       # play ~12s of each track
  python fetch_music.py --preview 3     # play one id or 1-based index
  python fetch_music.py --mix VIDEO --track auto
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
PACK_DIR = ROOT / "assets" / "music"
CATALOG_PATH = PACK_DIR / "catalog.json"
SUGGEST_DIR = PACK_DIR / "suggested"
USER_AGENT = "lolambrosek-music/1.0"
PREVIEW_S = 12.0


def load_catalog_doc() -> dict[str, Any]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def load_catalog() -> list[dict[str, Any]]:
    return list(load_catalog_doc().get("tracks") or [])


def pick_track(rng: random.Random | None = None) -> dict[str, Any]:
    """Uniform random pick from the catalog."""
    tracks = load_catalog()
    if not tracks:
        raise RuntimeError("empty music catalog")
    rng = rng or random.Random()
    return rng.choice(tracks)


def track_path(track: dict[str, Any]) -> Path:
    mid = int(track["mixkit_id"])
    slug = str(track["id"])
    return SUGGEST_DIR / f"{slug}_{mid}.mp3"


def resolve_track(query: str) -> dict[str, Any]:
    tracks = load_catalog()
    q = query.strip().lower()
    if q.isdigit():
        idx = int(q)
        if 1 <= idx <= len(tracks):
            return tracks[idx - 1]
        raise SystemExit(f"error: index {idx} is out of range 1–{len(tracks)}")
    for track in tracks:
        if str(track["id"]).lower() == q or str(track["name"]).lower() == q:
            return track
    raise SystemExit(f"error: unknown track {query!r}")


def resolve_or_pick(query: str) -> dict[str, Any]:
    q = str(query or "").strip().lower()
    if q in {"", "auto", "random"}:
        return pick_track()
    return resolve_track(q)


def download_track(track: dict[str, Any]) -> Path:
    dest = track_path(track)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 20_000:
        return dest
    url = str(track["url"])
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    print(f"[music] get {track['name']} → {dest.name}", flush=True)
    with urllib.request.urlopen(req, timeout=60) as resp:
        dest.write_bytes(resp.read())
    return dest


def suggest() -> list[Path]:
    out: list[Path] = []
    for track in load_catalog():
        out.append(download_track(track))
    print(f"[music] {len(out)} tracks in {SUGGEST_DIR}", flush=True)
    return out


def status() -> None:
    tracks = load_catalog()
    share = 1.0 / max(1, len(tracks))
    print(f"{len(tracks)} Mixkit chill/lofi beds  (license: Mixkit Free, no attribution)\n")
    for i, track in enumerate(tracks, 1):
        path = track_path(track)
        mark = "ready" if path.is_file() else "missing"
        print(
            f"  {i}. {track['id']:<20}  {track['name']} — {track['artist']}  "
            f"({track['genre']}, {track['duration']})  {share:.0%}  [{mark}]"
        )
    print("\nPreview:  python fetch_music.py --preview")
    print("Mix:      python fetch_music.py --mix VIDEO.mp4 --track auto")


def preview(query: str | None = None) -> None:
    tracks = load_catalog() if query is None else [resolve_track(query)]
    for i, track in enumerate(tracks, 1):
        path = download_track(track)
        label = track["id"] if query else f"{i}/{len(load_catalog())}  {track['id']}"
        print(
            f"\n▶  {label}   {track['name']} — {track['artist']}  ({track['duration']})",
            flush=True,
        )
        clip = Path(tempfile.gettempdir()) / f"lofi_preview_{track['id']}.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(path),
                "-t",
                f"{PREVIEW_S:.3f}",
                "-ac",
                "2",
                "-ar",
                "44100",
                str(clip),
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(["afplay", str(clip)], check=False)


def probe_duration(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or "ffprobe failed")
    return float((proc.stdout or "0").strip() or 0.0)


def mix_bed(
    video: Path,
    track: dict[str, Any],
    output: Path,
    *,
    music_db: float = -20.0,
    fade_in: float = 1.2,
    fade_out: float = 2.5,
) -> Path:
    """Loop a music bed under the video audio. Video stream is copied."""
    if not video.is_file():
        raise FileNotFoundError(video)
    music = download_track(track)
    dur = probe_duration(video)
    fade_out_at = max(0.0, dur - fade_out)
    output.parent.mkdir(parents=True, exist_ok=True)
    filt = (
        f"[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[game];"
        f"[1:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
        f"volume={music_db:.1f}dB,afade=t=in:st=0:d={fade_in:.3f},"
        f"afade=t=out:st={fade_out_at:.3f}:d={fade_out:.3f}[bed];"
        f"[game][bed]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[a]"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video),
        "-stream_loop",
        "-1",
        "-i",
        str(music),
        "-filter_complex",
        filt,
        "-map",
        "0:v",
        "-map",
        "[a]",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-shortest",
        "-movflags",
        "+faststart",
        str(output),
    ]
    print(
        f"[music] mix {track['id']} at {music_db:.0f} dB under {video.name} → {output.name}",
        flush=True,
    )
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"ffmpeg mix failed\n{detail}")
    return output


def mix_into(
    video: Path,
    track: dict[str, Any] | None = None,
    *,
    music_db: float = -20.0,
) -> dict[str, Any]:
    """Pick (if needed) and mix a bed onto ``video`` in place."""
    chosen = track if track is not None else pick_track()
    tmp = video.with_name(f"{video.stem}._music.mp4")
    try:
        mix_bed(video, chosen, tmp, music_db=music_db)
        tmp.replace(video)
    finally:
        tmp.unlink(missing_ok=True)
    print(f"[music] using {chosen['id']} ({chosen['name']}) at {music_db:.0f} dB", flush=True)
    return {"id": chosen["id"], "name": chosen["name"], "db": music_db}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Fetch and mix Mixkit lofi beds.")
    p.add_argument("--suggest", action="store_true", help="Download the catalog")
    p.add_argument(
        "--preview",
        nargs="?",
        const="",
        default=None,
        help="Play 12s samples (all, or a track id / number)",
    )
    p.add_argument("--mix", type=Path, default=None, help="Portrait / weave mp4 to mix under")
    p.add_argument("--track", default="auto", help="Catalog id, 1-based index, auto, or all")
    p.add_argument("--output", type=Path, default=None, help="Mix output (default: <video>_lofi.mp4)")
    p.add_argument("--music-db", type=float, default=-20.0, help="Bed level vs game audio (default -20)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mix is not None:
        video = args.mix.expanduser().resolve()
        if str(args.track).strip().lower() == "all":
            tracks = load_catalog()
            out_dir = args.output
            if out_dir is None:
                out_dir = video.with_name(video.stem + "_lofi_beds")
            else:
                out_dir = out_dir.expanduser()
            out_dir.mkdir(parents=True, exist_ok=True)
            made = []
            for track in tracks:
                dest = out_dir / f"{track['id']}.mp4"
                mix_bed(video, track, dest.resolve(), music_db=float(args.music_db))
                made.append({"track": track["id"], "output": str(dest)})
            print(json.dumps({"dir": str(out_dir), "mixes": made}, indent=2))
            return 0
        track = resolve_or_pick(str(args.track))
        out = args.output
        if out is None:
            out = video.with_name(video.stem + "_lofi.mp4")
        mix_bed(video, track, out.resolve(), music_db=float(args.music_db))
        print(json.dumps({"output": str(out), "track": track["id"]}, indent=2))
        return 0
    if args.suggest:
        suggest()
        return 0
    if args.preview is not None:
        preview(args.preview or None)
        return 0
    status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
