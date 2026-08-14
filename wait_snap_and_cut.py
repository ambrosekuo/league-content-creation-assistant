#!/usr/bin/env python3
"""After transcript.json exists, snap KILL/DEATH windows and cut into a new folder."""

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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    parser.add_argument("--timeout-minutes", type=float, default=240.0)
    parser.add_argument("--output-dir-name", default="lol_clips_snapped")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    dataset_dir = args.dataset_dir.resolve()
    transcript = dataset_dir / "transcript.json"
    snapped = dataset_dir / "lol_events_snapped.json"
    out_dir = dataset_dir / args.output_dir_name
    log_path = root / "logs" / f"snap_cut_{dataset_dir.name}.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[{utc_now()}] waiting for {transcript}", flush=True)
    started = time.time()
    while not transcript.is_file():
        if time.time() - started > args.timeout_minutes * 60:
            log_path.write_text(
                json.dumps({"ok": False, "error": "timeout waiting for transcript"}, indent=2)
                + "\n"
            )
            print("TIMEOUT", flush=True)
            return 1
        print(f"[{utc_now()}] still waiting for transcript…", flush=True)
        time.sleep(args.poll_seconds)

    # Ensure transcript looks complete (has segments).
    while True:
        try:
            payload = json.loads(transcript.read_text(encoding="utf-8"))
            if payload.get("segments") is not None and payload.get("segment_count", 0) >= 0:
                # Wait until file stable (transcribe writes at end, so existence is enough).
                if "segment_count" in payload:
                    break
        except json.JSONDecodeError:
            pass
        if time.time() - started > args.timeout_minutes * 60:
            log_path.write_text(
                json.dumps({"ok": False, "error": "timeout reading transcript"}, indent=2)
                + "\n"
            )
            return 1
        time.sleep(2)

    print(f"[{utc_now()}] transcript ready · snapping", flush=True)
    snap = subprocess.run(
        [
            sys.executable,
            str(root / "snap_clips_to_transcript.py"),
            "--dataset-dir",
            str(dataset_dir),
            "--types",
            "KILL,DEATH,ASSIST",
            "--center-on-event",
            "--pre-roll",
            "8",
            "--post-roll",
            "10",
            "--max-duration",
            "22",
            "--speech-nudge",
            "2",
            "--merge-gap",
            "0",
            "--overlap-merge",
            "--output",
            str(snapped),
        ],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    print(snap.stdout or "", end="")
    if snap.returncode != 0:
        print(snap.stderr or "", file=sys.stderr)
        log_path.write_text(
            json.dumps(
                {
                    "ok": False,
                    "step": "snap",
                    "stderr": snap.stderr,
                    "finishedAt": utc_now(),
                },
                indent=2,
            )
            + "\n"
        )
        return snap.returncode

    print(f"[{utc_now()}] cutting into {out_dir}", flush=True)
    cut = subprocess.run(
        [
            sys.executable,
            str(root / "cut_lol_clips.py"),
            "--dataset-dir",
            str(dataset_dir),
            "--from-windows",
            str(snapped),
            "--output-dir",
            str(out_dir),
            "--types",
            "KILL,DEATH",
            "--force",
            "--stream-copy",
        ],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    print(cut.stdout or "", end="")
    if cut.stderr:
        print(cut.stderr, file=sys.stderr, end="")

    report = {
        "ok": cut.returncode == 0,
        "dataset_dir": str(dataset_dir),
        "transcript": str(transcript),
        "snapped_events": str(snapped),
        "output_dir": str(out_dir),
        "original_clips_untouched": str(dataset_dir / "lol_clips"),
        "finishedAt": utc_now(),
        "snap_stdout": (snap.stdout or "")[-1000:],
        "cut_stdout_tail": "\n".join((cut.stdout or "").splitlines()[-30:]),
        "cut_stderr_tail": "\n".join((cut.stderr or "").splitlines()[-30:]),
    }
    if (out_dir / "clips.json").is_file():
        manifest = json.loads((out_dir / "clips.json").read_text(encoding="utf-8"))
        report["clip_count"] = manifest.get("clip_count")
    log_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"[{utc_now()}] wrote {log_path}", flush=True)
    return cut.returncode


if __name__ == "__main__":
    sys.exit(main())
