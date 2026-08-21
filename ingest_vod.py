#!/usr/bin/env python3
"""Ingest a Twitch VOD URL or local OBS recording into a stable dataset."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dataset_paths import dated_dir_name, find_dataset_dir, vod_id_from_dir_name
from storage_gcs import day_key_from_dt
from yt_dlp import YoutubeDL


TWITCH_VOD_RE = re.compile(r"(?:twitch\.tv/videos/|video/)(\d+)", re.IGNORECASE)


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and raise a readable error on failure."""
    try:
        return subprocess.run(
            command,
            check=True,
            text=True,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Required executable was not found: {command[0]}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise RuntimeError(f"Command failed: {' '.join(command)}\n{message}") from exc


def safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    if not cleaned:
        raise ValueError("The supplied ID did not contain usable characters.")
    return cleaned


def derive_vod_id(url: str) -> str | None:
    match = TWITCH_VOD_RE.search(url)
    return match.group(1) if match else None


def probe_media(path: Path) -> dict[str, Any]:
    result = run([
        "ffprobe",
        "-v", "error",
        "-show_format",
        "-show_streams",
        "-of", "json",
        str(path),
    ])
    return json.loads(result.stdout)


def extract_audio(source: Path, output: Path, force: bool) -> None:
    if output.exists() and not force:
        return

    command = [
        "ffmpeg",
        "-y" if force else "-n",
        "-i", str(source),
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        str(output),
    ]
    run(command)


def find_downloaded_source(folder: Path) -> Path:
    ignored_suffixes = {
        ".json", ".jpg", ".jpeg", ".png", ".webp",
        ".part", ".ytdl", ".description",
    }
    candidates = [
        path for path in folder.iterdir()
        if path.is_file()
        and path.name not in {"audio.wav", "ingest.json", "metadata.json"}
        and path.suffix.lower() not in ignored_suffixes
    ]

    if not candidates:
        raise RuntimeError("yt-dlp finished but no source media file was found.")

    return max(candidates, key=lambda path: path.stat().st_size)


def format_section_spec(start_seconds: float, end_seconds: float) -> str:
    """yt-dlp --download-sections value, e.g. *1:00:00-4:00:00."""

    def hms(total: float) -> str:
        s = max(0, int(total))
        hours, rem = divmod(s, 3600)
        minutes, secs = divmod(rem, 60)
        return f"{hours}:{minutes:02d}:{secs:02d}"

    return f"*{hms(start_seconds)}-{hms(end_seconds)}"


def fetch_vod_metadata(url: str) -> dict[str, Any]:
    """Resolve VOD info without downloading media."""
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
        return ydl.sanitize_info(info)


def download_vod(
    url: str,
    folder: Path,
    cookies_from_browser: str | None,
    force: bool,
    *,
    format_selector: str = "best",
    section_start: float | None = None,
    section_end: float | None = None,
) -> tuple[Path, dict[str, Any]]:
    output_template = str(folder / "source.%(ext)s")

    options: dict[str, Any] = {
        "format": format_selector,
        "outtmpl": output_template,
        "writethumbnail": True,
        "writeinfojson": True,
        "overwrites": force,
        "continuedl": True,
        "noplaylist": True,
        "quiet": False,
        "no_warnings": False,
        # FixupM3u8 remuxes HLS → mp4 via ffmpeg into a *.temp.mp4 alongside the
        # download. On Cloud Run that means ~2× file size in memory-backed /tmp
        # (17GiB VOD → SIGBUS). Skip fixup; ffmpeg stream-copy cuts still work.
        "fixup": "never",
    }
    if section_start is not None and section_end is not None:
        # Python API uses download_ranges (not the CLI --download-sections string list).
        from yt_dlp.utils import download_range_func

        spec = format_section_spec(section_start, section_end)
        options["download_ranges"] = download_range_func(
            None, [(float(section_start), float(section_end))]
        )
        # Avoid ffmpeg re-encode (force_keyframes_at_cuts); keep fragment speed.
        print(
            f"[ingest] section {spec} ({section_start:.0f}s–{section_end:.0f}s)",
            flush=True,
        )

    if cookies_from_browser:
        # yt-dlp expects a tuple such as ("chrome",) or ("firefox",).
        options["cookiesfrombrowser"] = (cookies_from_browser,)

    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        sanitized = ydl.sanitize_info(info)

    source = find_downloaded_source(folder)
    return source, sanitized


def ingest_local(
    local_file: Path,
    folder: Path,
    copy_file: bool,
    force: bool,
) -> Path:
    source = local_file.resolve()

    if not source.is_file():
        raise FileNotFoundError(f"Local recording does not exist: {source}")

    if not copy_file:
        return source

    destination = folder / f"source{source.suffix.lower()}"

    if destination.exists():
        if force:
            destination.unlink()
        else:
            return destination

    shutil.copy2(source, destination)
    return destination


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download/register a Twitch VOD and extract transcription audio."
    )
    parser.add_argument(
        "url",
        nargs="?",
        help="Twitch VOD URL, such as https://www.twitch.tv/videos/1234567890",
    )
    parser.add_argument(
        "--local-file",
        type=Path,
        help="Use a local OBS recording instead of downloading a Twitch VOD.",
    )
    parser.add_argument(
        "--id",
        help="Dataset ID. Required for local files unless derivable from the filename.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("data"),
        help="Root output directory. Default: ./data",
    )
    parser.add_argument(
        "--cookies-from-browser",
        choices=["chrome", "chromium", "edge", "firefox", "opera", "safari", "vivaldi", "brave"],
        help="Read authentication cookies from your own browser profile.",
    )
    parser.add_argument(
        "--no-copy",
        action="store_true",
        help="For local recordings, reference the original instead of copying it.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite/redownload existing outputs.",
    )
    parser.add_argument(
        "--format",
        default=None,
        help=(
            "yt-dlp format selector (default: env YTDLP_FORMAT or 'best'). "
            "Cloud jobs should use e.g. best[height<=720]/best[height<=720]/best."
        ),
    )
    parser.add_argument(
        "--skip-audio",
        action="store_true",
        help="Skip PCM audio.wav extraction (cloud archive does not need it).",
    )
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="Fetch yt-dlp metadata.json only (no media download).",
    )
    parser.add_argument(
        "--section-start",
        type=float,
        default=None,
        help="Download only this VOD window start (seconds).",
    )
    parser.add_argument(
        "--section-end",
        type=float,
        default=None,
        help="Download only this VOD window end (seconds).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if bool(args.url) == bool(args.local_file):
        print("Provide exactly one of: a Twitch VOD URL or --local-file.", file=sys.stderr)
        return 2

    if args.url:
        dataset_id = args.id or derive_vod_id(args.url)
        if not dataset_id:
            print("Could not derive the Twitch video ID; pass --id.", file=sys.stderr)
            return 2
    else:
        dataset_id = args.id or args.local_file.stem

    vod_id = safe_id(dataset_id)
    output_root = args.output_root.resolve()
    folder = find_dataset_dir(output_root, vod_id)
    early_meta: dict[str, Any] | None = None

    if folder is None:
        if args.url:
            early_meta = fetch_vod_metadata(args.url)
            ts = early_meta.get("timestamp")
            if ts is not None:
                day_key = day_key_from_dt(
                    datetime.fromtimestamp(int(ts), tz=timezone.utc)
                )
                folder = output_root / dated_dir_name(day_key, vod_id)
            else:
                folder = output_root / vod_id
        elif args.local_file:
            day_key = day_key_from_dt(
                datetime.fromtimestamp(args.local_file.stat().st_mtime, tz=timezone.utc)
            )
            folder = output_root / dated_dir_name(day_key, vod_id)
        else:
            folder = output_root / vod_id

    folder.mkdir(parents=True, exist_ok=True)
    dataset_id = vod_id_from_dir_name(folder.name)

    try:
        extractor_metadata: dict[str, Any] | None = early_meta

        format_selector = (
            args.format
            or os.environ.get("YTDLP_FORMAT")
            or "best"
        )

        if args.url and args.metadata_only:
            print("[ingest] metadata-only", flush=True)
            extractor_metadata = extractor_metadata or fetch_vod_metadata(args.url)
            (folder / "metadata.json").write_text(
                json.dumps(extractor_metadata, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "dataset_id": dataset_id,
                        "folder": str(folder),
                        "duration": extractor_metadata.get("duration"),
                        "title": extractor_metadata.get("title"),
                    },
                    indent=2,
                )
            )
            return 0

        if args.url:
            if (args.section_start is None) ^ (args.section_end is None):
                print("Provide both --section-start and --section-end.", file=sys.stderr)
                return 2
            print(f"[ingest] yt-dlp format={format_selector}", flush=True)
            source, extractor_metadata = download_vod(
                url=args.url,
                folder=folder,
                cookies_from_browser=args.cookies_from_browser,
                force=args.force,
                format_selector=format_selector,
                section_start=args.section_start,
                section_end=args.section_end,
            )
            (folder / "metadata.json").write_text(
                json.dumps(extractor_metadata, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            if args.section_start is not None and args.section_end is not None:
                (folder / "section.json").write_text(
                    json.dumps(
                        {
                            "start": args.section_start,
                            "end": args.section_end,
                            "spec": format_section_spec(
                                args.section_start, args.section_end
                            ),
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            source_type = "twitch_vod"
        else:
            source = ingest_local(
                local_file=args.local_file,
                folder=folder,
                copy_file=not args.no_copy,
                force=args.force,
            )
            source_type = "local_recording"

        media_probe = probe_media(source)
        audio_path = folder / "audio.wav"
        if args.skip_audio:
            print("[ingest] skipping audio.wav extraction", flush=True)
        else:
            extract_audio(source, audio_path, force=args.force)

        duration_string = media_probe.get("format", {}).get("duration")
        duration_seconds = float(duration_string) if duration_string else None

        manifest = {
            "schema_version": 1,
            "dataset_id": dataset_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_type": source_type,
            "source_url": args.url,
            "source_path": str(source),
            "audio_path": str(audio_path) if audio_path.is_file() else None,
            "ytdlp_format": format_selector if args.url else None,
            "duration_seconds": duration_seconds,
            "ffprobe": media_probe,
        }

        (folder / "ingest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        print(json.dumps({
            "status": "ok",
            "dataset_id": dataset_id,
            "folder": str(folder),
            "source": str(source),
            "audio": str(audio_path),
            "duration_seconds": duration_seconds,
        }, indent=2))
        return 0

    except Exception as exc:
        print(f"Ingestion failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
