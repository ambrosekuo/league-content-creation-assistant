#!/usr/bin/env python3
"""Ingest a Twitch VOD URL or local OBS recording into a stable dataset."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


def download_vod(
    url: str,
    folder: Path,
    cookies_from_browser: str | None,
    force: bool,
) -> tuple[Path, dict[str, Any]]:
    output_template = str(folder / "source.%(ext)s")

    options: dict[str, Any] = {
        "format": "best",
        "outtmpl": output_template,
        "writethumbnail": True,
        "writeinfojson": True,
        "overwrites": force,
        "continuedl": True,
        "noplaylist": True,
        "quiet": False,
        "no_warnings": False,
    }

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

    dataset_id = safe_id(dataset_id)
    folder = args.output_root.resolve() / dataset_id
    folder.mkdir(parents=True, exist_ok=True)

    try:
        extractor_metadata: dict[str, Any] | None = None

        if args.url:
            source, extractor_metadata = download_vod(
                url=args.url,
                folder=folder,
                cookies_from_browser=args.cookies_from_browser,
                force=args.force,
            )
            (folder / "metadata.json").write_text(
                json.dumps(extractor_metadata, indent=2, ensure_ascii=False),
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
            "audio_path": str(audio_path),
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
