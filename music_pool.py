#!/usr/bin/env python3
"""Vetted music pool for automated League clips.

Curate 20–40 tracks with mood/energy/category tags, then let the pipeline pick
per clip classification instead of hunting new stock music every time.

  python music_pool.py                         # pool status
  python music_pool.py --pick outplay          # test category selection
  python music_pool.py --pick-clip clip.json   # use classifier output
  python music_pool.py --adopt TRACK.mp3 --id my-phonk --energy 0.85 \\
      --categories outplay,multikill --mood dark,aggressive --source uppbeat
  python music_pool.py --mix VIDEO.mp4 --category multikill
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from clip_classifiers.taxonomy import PRIMARY_CATEGORIES, normalize_category

ROOT = Path(__file__).resolve().parent
MUSIC_DIR = ROOT / "assets" / "music"
POOL_PATH = MUSIC_DIR / "pool.json"
INBOX_DIR = MUSIC_DIR / "inbox"
AUDIO_SUFFIX = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac"}

# Clip type → target energy band + mood hints for scoring.
CATEGORY_PROFILE: dict[str, dict[str, Any]] = {
    "outplay": {"energy": (0.72, 0.95), "moods": {"dark", "aggressive", "confident", "energetic"}},
    "multikill": {"energy": (0.78, 1.0), "moods": {"energetic", "bass", "aggressive", "bright"}},
    "chase": {"energy": (0.78, 1.0), "moods": {"energetic", "hypnotic", "uplifting"}},
    "mistake": {"energy": (0.55, 0.78), "moods": {"bounce", "playful", "goofy", "fun"}},
    "survival": {"energy": (0.65, 0.88), "moods": {"tense", "uplifting", "melodic", "atmospheric"}},
    "game_end": {"energy": (0.45, 0.72), "moods": {"melodic", "atmospheric", "calm"}},
    "reaction": {"energy": (0.15, 0.42), "moods": {"soft", "lofi", "calm", "minimal"}},
    "ordinary": {"energy": (0.2, 0.45), "moods": {"chill", "lofi", "calm", "warm"}},
    "decision": {"energy": (0.35, 0.58), "moods": {"calm", "warm", "melodic"}},
    "trade": {"energy": (0.6, 0.82), "moods": {"confident", "dark", "worth"}},
}


def load_pool() -> dict[str, Any]:
    if not POOL_PATH.is_file():
        raise FileNotFoundError(f"missing pool catalog: {POOL_PATH}")
    return json.loads(POOL_PATH.read_text(encoding="utf-8"))


def save_pool(doc: dict[str, Any]) -> None:
    POOL_PATH.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def enabled_tracks(doc: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    doc = doc or load_pool()
    out: list[dict[str, Any]] = []
    for track in doc.get("tracks") or []:
        if track.get("enabled", True) is False:
            continue
        out.append(track)
    return out


def list_tracks_public() -> list[dict[str, Any]]:
    """Pool tracks for the review UI picker."""
    try:
        tracks = enabled_tracks()
    except FileNotFoundError:
        return []
    out: list[dict[str, Any]] = []
    for track in tracks:
        out.append(
            {
                "id": str(track.get("id") or ""),
                "name": str(track.get("name") or track.get("id") or ""),
                "mood": list(track.get("mood") or []),
                "energy": track.get("energy"),
                "categories": list(track.get("categories") or []),
                "bpm": track.get("bpm"),
                "source": track.get("source"),
                "ready": track_ready(track),
            }
        )
    return out


def track_by_id(track_id: str, doc: dict[str, Any] | None = None) -> dict[str, Any] | None:
    tid = str(track_id or "").strip()
    if not tid:
        return None
    for track in enabled_tracks(doc):
        if str(track.get("id") or "") == tid:
            return track
    return None


def track_file(track: dict[str, Any]) -> Path:
    rel = str(track.get("file") or "").strip()
    if not rel:
        raise ValueError(f"track {track.get('id')!r} has no file")
    return (MUSIC_DIR / rel).resolve()


def track_ready(track: dict[str, Any]) -> bool:
    try:
        path = track_file(track)
    except ValueError:
        return False
    return path.is_file() and path.stat().st_size > 20_000


def _energy_fit(energy: float, band: tuple[float, float]) -> float:
    lo, hi = band
    mid = (lo + hi) / 2.0
    half = max(0.08, (hi - lo) / 2.0)
    return max(0.0, 1.0 - abs(energy - mid) / half)


def score_track(track: dict[str, Any], *, category: str) -> float:
    cat = normalize_category(category)
    profile = CATEGORY_PROFILE.get(cat, CATEGORY_PROFILE["ordinary"])
    energy = float(track.get("energy") or 0.5)
    moods = {str(m).lower() for m in (track.get("mood") or [])}
    cats = {normalize_category(c) for c in (track.get("categories") or [])}

    score = 0.0
    if cat in cats:
        score += 3.0
    score += 2.5 * _energy_fit(energy, profile["energy"])
    mood_hits = len(moods & set(profile["moods"]))
    score += min(1.5, mood_hits * 0.5)
    fit = _energy_fit(energy, profile["energy"])
    if fit < 0.35:
        score -= 2.5
    if not track_ready(track):
        score -= 5.0
    return score


def pick_for_category(
    category: str,
    *,
    secondary: list[str] | None = None,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Weighted random pick from tracks that fit a clip category."""
    rng = rng or random.Random()
    primary = normalize_category(category)
    candidates = enabled_tracks()
    if not candidates:
        raise RuntimeError("music pool is empty")

    scored: list[tuple[float, dict[str, Any]]] = []
    for track in candidates:
        base = score_track(track, category=primary)
        for sec in secondary or []:
            if normalize_category(sec) in {normalize_category(c) for c in (track.get("categories") or [])}:
                base += 0.75
        if base > 0:
            scored.append((base, track))

    if not scored:
        ready = [t for t in candidates if track_ready(t)] or candidates
        return rng.choice(ready)

    scored.sort(key=lambda item: item[0], reverse=True)
    top = scored[: min(5, len(scored))]
    weights = [max(0.05, s) for s, _ in top]
    chosen = rng.choices([t for _, t in top], weights=weights, k=1)[0]
    return chosen


def pick_for_clip(
    interpretation: dict[str, Any],
    *,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Pick from pool using classifier ``interpretation`` record."""
    primary = normalize_category(interpretation.get("primary") or interpretation.get("category"))
    secondary = list(interpretation.get("secondary") or [])
    return pick_for_category(primary, secondary=secondary, rng=rng)


def slugify_id(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return text[:48] or "track"


def adopt_track(
    source: Path,
    *,
    track_id: str,
    name: str | None = None,
    source_label: str = "inbox",
    license_label: str = "unknown",
    mood: list[str] | None = None,
    energy: float = 0.5,
    categories: list[str] | None = None,
    bpm: int | None = None,
) -> dict[str, Any]:
    """Copy a downloaded track into inbox/ and register it in pool.json."""
    src = source.expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(src)
    if src.suffix.lower() not in AUDIO_SUFFIX:
        raise ValueError(f"unsupported audio type: {src.suffix}")

    tid = slugify_id(track_id or src.stem)
    dest_name = f"{tid}{src.suffix.lower()}"
    dest = INBOX_DIR / dest_name
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)

    cats = [normalize_category(c) for c in (categories or ["ordinary"])]
    entry = {
        "id": tid,
        "file": f"inbox/{dest_name}",
        "name": name or src.stem,
        "source": source_label,
        "license": license_label,
        "mood": [str(m).strip().lower() for m in (mood or []) if str(m).strip()],
        "energy": round(float(energy), 2),
        "categories": cats,
        "enabled": True,
    }
    if bpm is not None:
        entry["bpm"] = int(bpm)

    doc = load_pool()
    tracks = list(doc.get("tracks") or [])
    tracks = [t for t in tracks if str(t.get("id")) != tid]
    tracks.append(entry)
    doc["tracks"] = tracks
    save_pool(doc)
    print(f"[pool] adopted {dest.name} as {tid}", flush=True)
    return entry


def pool_status() -> None:
    doc = load_pool()
    tracks = enabled_tracks(doc)
    ready = sum(1 for t in tracks if track_ready(t))
    print(f"{len(tracks)} tracks in pool ({ready} files on disk)\n")
    by_cat: dict[str, int] = {c: 0 for c in PRIMARY_CATEGORIES}
    for track in tracks:
        for cat in track.get("categories") or []:
            norm = normalize_category(str(cat))
            by_cat[norm] = by_cat.get(norm, 0) + 1
    print("Coverage by clip category:")
    for cat in PRIMARY_CATEGORIES:
        print(f"  {cat:<12} {by_cat.get(cat, 0)}")
    print("\nTracks:")
    for i, track in enumerate(tracks, 1):
        mark = "ready" if track_ready(track) else "missing"
        cats = ", ".join(track.get("categories") or [])
        moods = ", ".join(track.get("mood") or [])
        print(
            f"  {i:>2}. {track['id']:<28} E={track.get('energy', '?'):<4}  "
            f"[{cats}]  {moods}  ({track.get('source')})  [{mark}]"
        )
    print("\nAdopt:    python music_pool.py --adopt ~/Downloads/track.mp3 --id my-track ...")
    print("Pick:     python music_pool.py --pick outplay")
    print("Mix:      python music_pool.py --mix clip.mp4 --category multikill")


def mix_pool_bed(
    video: Path,
    track: dict[str, Any],
    output: Path,
    *,
    music_db: float = -18.0,
    fade_in: float = 0.8,
    fade_out: float = 2.0,
) -> Path:
    from fetch_music import probe_duration

    audio = track_file(track)
    if not audio.is_file():
        raise FileNotFoundError(f"missing pool audio: {audio}")
    dur = probe_duration(video)
    fade_out_at = max(0.0, dur - fade_out)
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
        str(audio),
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
        f"[pool] mix {track['id']} at {music_db:.0f} dB under {video.name} → {output.name}",
        flush=True,
    )
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"ffmpeg mix failed\n{detail}")
    return output


def resolve_pool_track(query: str, *, category: str = "multikill") -> dict[str, Any]:
    """Resolve a pool track id, or pick for ``category`` when auto/random."""
    q = str(query or "").strip().lower()
    if q.startswith("pool:"):
        q = q.split(":", 1)[1].strip() or "auto"
    if q in {"", "auto", "random", "pool"}:
        cat = str(category or "multikill").strip().lower()
        if cat in {"", "auto"}:
            cat = "multikill"
        return pick_for_category(cat)
    matches = [t for t in enabled_tracks() if str(t.get("id")).lower() == q]
    if not matches:
        raise ValueError(f"unknown pool track {query!r}")
    return matches[0]


def mix_pool_into(
    video: Path,
    track: dict[str, Any] | None = None,
    *,
    category: str = "multikill",
    music_db: float = -18.0,
) -> dict[str, Any]:
    """Pick (if needed) and mix a pool bed onto ``video`` in place."""
    chosen = track if track is not None else resolve_pool_track("auto", category=category)
    tmp = video.with_name(f"{video.stem}._pool_music.mp4")
    try:
        mix_pool_bed(video, chosen, tmp, music_db=float(music_db))
        tmp.replace(video)
    finally:
        tmp.unlink(missing_ok=True)
    print(
        f"[pool] using {chosen['id']} ({chosen['name']}) at {music_db:.0f} dB",
        flush=True,
    )
    return {
        "id": chosen["id"],
        "name": chosen["name"],
        "db": music_db,
        "source": "pool",
        "file": str(track_file(chosen)),
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Vetted music pool for automated clips.")
    p.add_argument("--pick", metavar="CATEGORY", help="Pick a track for a clip category")
    p.add_argument("--pick-clip", type=Path, help="JSON file with interpretation.primary")
    p.add_argument("--mix", type=Path, help="Mix a pool track under a video")
    p.add_argument("--category", default="auto", help="Category for --mix (or auto → outplay)")
    p.add_argument("--track", default="auto", help="Pool track id, or auto")
    p.add_argument("--output", type=Path, help="Mix output path")
    p.add_argument("--music-db", type=float, default=-18.0)
    p.add_argument("--adopt", type=Path, help="Register a downloaded MP3/WAV into the pool")
    p.add_argument("--id", dest="track_id", help="Pool id for --adopt")
    p.add_argument("--name", help="Display name for --adopt")
    p.add_argument("--source", default="inbox", help="Source label (uppbeat, streambeats, …)")
    p.add_argument("--license", default="unknown", help="License label")
    p.add_argument("--mood", help="Comma-separated moods")
    p.add_argument("--energy", type=float, default=0.5)
    p.add_argument("--categories", help="Comma-separated clip categories")
    p.add_argument("--bpm", type=int)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.adopt is not None:
        if not args.track_id:
            raise SystemExit("error: --adopt requires --id")
        mood = [m.strip() for m in str(args.mood or "").split(",") if m.strip()]
        cats = [c.strip() for c in str(args.categories or "ordinary").split(",") if c.strip()]
        entry = adopt_track(
            args.adopt,
            track_id=args.track_id,
            name=args.name,
            source_label=str(args.source),
            license_label=str(args.license),
            mood=mood,
            energy=float(args.energy),
            categories=cats,
            bpm=args.bpm,
        )
        print(json.dumps(entry, indent=2))
        return 0

    if args.pick is not None:
        track = pick_for_category(args.pick)
        print(json.dumps({**track, "path": str(track_file(track))}, indent=2))
        return 0

    if args.pick_clip is not None:
        payload = json.loads(args.pick_clip.read_text(encoding="utf-8"))
        interpretation = payload.get("interpretation") or payload
        track = pick_for_clip(interpretation)
        print(json.dumps({**track, "path": str(track_file(track))}, indent=2))
        return 0

    if args.mix is not None:
        video = args.mix.expanduser().resolve()
        mode = str(args.track).strip().lower()
        if mode in {"", "auto", "random"}:
            cat = str(args.category).strip().lower()
            if cat in {"", "auto"}:
                cat = "outplay"
            track = pick_for_category(cat)
        else:
            matches = [t for t in enabled_tracks() if str(t.get("id")) == mode]
            if not matches:
                raise SystemExit(f"error: unknown pool track {mode!r}")
            track = matches[0]
        out = args.output or video.with_name(f"{video.stem}_pool_{track['id']}.mp4")
        mix_pool_bed(video, track, out.expanduser().resolve(), music_db=float(args.music_db))
        print(json.dumps({"output": str(out), "track": track["id"]}, indent=2))
        return 0

    pool_status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
