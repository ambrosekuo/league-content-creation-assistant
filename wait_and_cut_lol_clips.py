#!/usr/bin/env python3
"""Wait for a VOD ingest to finish, then run cut_lol_clips.py."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ingest_ready(dataset_dir: Path) -> Path | None:
    ingest_path = dataset_dir / "ingest.json"
    if not ingest_path.is_file():
        return None
    try:
        ingest = json.loads(ingest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    source = Path(ingest.get("source_path") or "")
    if source.is_file() and source.stat().st_size > 0:
        # Still downloading if sibling .part / .ytdl exist.
        if (dataset_dir / f"{source.name}.part").exists():
            return None
        if list(dataset_dir.glob(f"{source.name}.part*")):
            return None
        if (dataset_dir / f"{source.name}.ytdl").exists():
            return None
        return source
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    parser.add_argument("--timeout-minutes", type=float, default=180.0)
    parser.add_argument("--max-clips", type=int, default=0)
    parser.add_argument("--types", default="KILL,DEATH,ASSIST")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    dataset_dir = args.dataset_dir.resolve()
    root = Path(__file__).resolve().parent
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"wait_and_cut_{dataset_dir.name}.json"

    started = time.time()
    timeout_s = args.timeout_minutes * 60
    print(f"[{utc_now()}] waiting for ingest in {dataset_dir}", flush=True)

    source: Path | None = None
    while True:
        source = ingest_ready(dataset_dir)
        if source is not None:
            break
        elapsed = time.time() - started
        if elapsed > timeout_s:
            report = {
                "ok": False,
                "error": f"timed out after {args.timeout_minutes} minutes",
                "dataset_dir": str(dataset_dir),
                "finishedAt": utc_now(),
            }
            log_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(f"[{utc_now()}] TIMEOUT", flush=True)
            return 1

        part = next(iter(sorted(dataset_dir.glob("source.mp4.part*"))), None)
        size = part.stat().st_size if part and part.is_file() else 0
        print(
            f"[{utc_now()}] still downloading… "
            f"{size / (1024**3):.2f} GiB partial · waited {elapsed/60:.1f}m",
            flush=True,
        )
        time.sleep(args.poll_seconds)

    print(f"[{utc_now()}] ingest ready: {source}", flush=True)
    cmd = [
        sys.executable,
        str(root / "cut_lol_clips.py"),
        "--dataset-dir",
        str(dataset_dir),
        "--types",
        args.types,
    ]
    if args.max_clips:
        cmd.extend(["--max-clips", str(args.max_clips)])
    if args.force:
        cmd.append("--force")

    proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True)
    report = {
        "ok": proc.returncode == 0,
        "dataset_dir": str(dataset_dir),
        "source_path": str(source),
        "finishedAt": utc_now(),
        "returncode": proc.returncode,
        "stdoutTail": "\n".join((proc.stdout or "").splitlines()[-40:]),
        "stderrTail": "\n".join((proc.stderr or "").splitlines()[-40:]),
    }
    clips_manifest = dataset_dir / "lol_clips" / "clips.json"
    if clips_manifest.is_file():
        try:
            manifest = json.loads(clips_manifest.read_text(encoding="utf-8"))
            report["clip_count"] = manifest.get("clip_count")
            report["clips_manifest"] = str(clips_manifest)
        except json.JSONDecodeError:
            pass

    log_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(proc.stdout or "", end="")
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, end="")
    print(f"[{utc_now()}] wrote {log_path}", flush=True)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
