#!/usr/bin/env python3

import argparse
import subprocess
from pathlib import Path

from ffmpeg_color import VIDEO_TO_BT709, X264_BT709


def timestamp_to_seconds(value: str) -> float:
    parts = value.split(":")

    if len(parts) == 1:
        return float(parts[0])

    if len(parts) == 2:
        minutes, seconds = parts
        return int(minutes) * 60 + float(seconds)

    if len(parts) == 3:
        hours, minutes, seconds = parts
        return (
            int(hours) * 3600
            + int(minutes) * 60
            + float(seconds)
        )

    raise ValueError(f"Invalid timestamp: {value}")


def main():
    parser = argparse.ArgumentParser(
        description="Extract a short from an existing portrait video."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("-o", "--output", type=Path)

    args = parser.parse_args()

    if not args.input.is_file():
        raise FileNotFoundError(args.input)

    start = timestamp_to_seconds(args.start)
    end = timestamp_to_seconds(args.end)

    if end <= start:
        raise ValueError("End must be after start")

    duration = end - start

    output = args.output or args.input.with_name(
        f"{args.input.stem}_short_{int(start)}_{int(end)}.mp4"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(args.input),
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
        "-vf",
        VIDEO_TO_BT709,
        *X264_BT709,
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output),
    ]

    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)

    print(f"\nCreated: {output}")
    print(f"Range: {start:.2f}s -> {end:.2f}s")
    print(f"Length: {duration:.2f}s")


if __name__ == "__main__":
    main()