#!/usr/bin/env python3
"""
Cloud Run Job entrypoint for Twitch VOD archive + LoL indexing.

Modes are explicit so a local download under data/ is never touched unless
you pass that path yourself.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from env_loader import load_dotenv


ROOT = Path(__file__).resolve().parent


def _run(cmd: list[str], *, cwd: Path | None = None) -> None:
    print("+", " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=str(cwd or ROOT), text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd)}")


def cmd_list_vods(args: argparse.Namespace) -> int:
    limit = str(args.limit)
    out_flags = ["--json"] if args.json else []
    _run([sys.executable, str(ROOT / "list_vods.py"), "--limit", limit, *out_flags])
    return 0


def cmd_gcs_list(args: argparse.Namespace) -> int:
    import storage_gcs as gcs

    ids = gcs.list_vod_ids()
    print(json.dumps({"bucket": gcs.bucket_name(), "prefix": gcs.prefix(), "vodIds": ids}, indent=2))
    return 0


def cmd_upload_dataset(args: argparse.Namespace) -> int:
    import storage_gcs as gcs

    dataset_dir = Path(args.dataset_dir).resolve()
    # Refuse to upload an in-progress download (partial fragments present).
    partials = list(dataset_dir.glob("*.part")) + list(dataset_dir.glob("*.ytdl"))
    partials += list(dataset_dir.glob("*.part-*"))
    if partials and not args.allow_partial:
        print(
            "error: dataset looks like an in-progress download "
            f"({len(partials)} partial file(s)). "
            "Wait for ingest to finish, or pass --allow-partial (not recommended).",
            file=sys.stderr,
        )
        return 2

    uploaded = gcs.upload_dataset_dir(dataset_dir, vod_id=args.vod_id)
    print(json.dumps({"status": "ok", "count": len(uploaded), "objects": uploaded}, indent=2))
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    """Run lol_indexer against a dataset dir; optionally upload events to GCS."""
    dataset_dir = Path(args.dataset_dir).resolve()
    events_out = dataset_dir / "lol_events.json"
    indexer = ROOT / "lol-indexer" / "lol_indexer.py"
    _run(
        [
            sys.executable,
            str(indexer),
            "--vod-dir",
            str(dataset_dir),
            "--output",
            str(events_out),
        ],
        cwd=ROOT / "lol-indexer",
    )

    if args.upload:
        import storage_gcs as gcs

        vod_id = (args.vod_id or dataset_dir.name).strip().lstrip("v")
        uri = gcs.upload_file(
            events_out,
            f"{gcs.vod_prefix(vod_id)}/lol_events.json",
            content_type="application/json",
        )
        print(json.dumps({"status": "ok", "events": str(events_out), "gcs": uri}, indent=2))
    else:
        print(json.dumps({"status": "ok", "events": str(events_out)}, indent=2))
    return 0


def cmd_nightly(args: argparse.Namespace) -> int:
    """
    List recent Twitch VODs, archive any missing from GCS into WORK_DIR, index, upload.

    Uses WORK_DIR (default /tmp/vod-work) — never the repo data/ folder unless set.
    """
    import storage_gcs as gcs
    from list_vods import get_app_access_token, get_user_id, list_archives

    work_dir = Path(os.environ.get("WORK_DIR") or args.work_dir or "/tmp/vod-work")
    work_dir.mkdir(parents=True, exist_ok=True)

    client_id = os.environ["TWITCH_CLIENT_ID"]
    client_secret = os.environ["TWITCH_CLIENT_SECRET"]
    channel = os.environ["TWITCH_CHANNEL"]

    token = get_app_access_token(client_id, client_secret)
    user = get_user_id(client_id, token, channel)
    videos = list_archives(client_id, token, str(user["id"]), limit=args.limit)

    results: list[dict[str, Any]] = []
    for vod in videos:
        vod_id = str(vod["id"])
        url = vod.get("url") or f"https://www.twitch.tv/videos/{vod_id}"
        entry: dict[str, Any] = {"vodId": vod_id, "url": url}

        if gcs.vod_archived(vod_id) and not args.force:
            entry["status"] = "skipped_already_archived"
            results.append(entry)
            print(f"[skip] {vod_id} already in GCS", flush=True)
            continue

        if args.dry_run:
            entry["status"] = "would_process"
            results.append(entry)
            print(f"[dry-run] would process {vod_id}", flush=True)
            continue

        dataset_dir = work_dir / vod_id
        dataset_dir.mkdir(parents=True, exist_ok=True)
        print(f"[ingest] {vod_id} → {dataset_dir}", flush=True)
        _run(
            [
                sys.executable,
                str(ROOT / "ingest_vod.py"),
                url,
                "--id",
                vod_id,
                "--output-root",
                str(work_dir),
            ]
        )

        print(f"[index] {vod_id}", flush=True)
        events_out = dataset_dir / "lol_events.json"
        _run(
            [
                sys.executable,
                str(ROOT / "lol-indexer" / "lol_indexer.py"),
                "--vod-dir",
                str(dataset_dir),
                "--output",
                str(events_out),
            ],
            cwd=ROOT / "lol-indexer",
        )

        print(f"[upload] {vod_id}", flush=True)
        uploaded = gcs.upload_dataset_dir(dataset_dir, vod_id=vod_id)
        entry["status"] = "archived"
        entry["uploaded"] = len(uploaded)
        results.append(entry)

        if args.cleanup:
            # Remove local work copy after successful upload (cloud /tmp hygiene).
            import shutil

            shutil.rmtree(dataset_dir, ignore_errors=True)

    summary = {
        "channel": channel,
        "workDir": str(work_dir),
        "results": results,
    }
    print(json.dumps(summary, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cloud Run job entrypoint")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list-vods", help="List Twitch archive VODs")
    p_list.add_argument("--limit", type=int, default=10)
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list_vods)

    p_gcs = sub.add_parser("gcs-list", help="List VOD ids already in GCS")
    p_gcs.set_defaults(func=cmd_gcs_list)

    p_up = sub.add_parser("upload-dataset", help="Upload a completed local dataset to GCS")
    p_up.add_argument("--dataset-dir", type=Path, required=True)
    p_up.add_argument("--vod-id", default=None)
    p_up.add_argument(
        "--allow-partial",
        action="store_true",
        help="Allow upload even if *.part files exist (dangerous)",
    )
    p_up.set_defaults(func=cmd_upload_dataset)

    p_idx = sub.add_parser("index", help="Index LoL events for a dataset dir")
    p_idx.add_argument("--dataset-dir", type=Path, required=True)
    p_idx.add_argument("--vod-id", default=None)
    p_idx.add_argument("--upload", action="store_true", help="Upload lol_events.json to GCS")
    p_idx.set_defaults(func=cmd_index)

    p_night = sub.add_parser(
        "nightly",
        help="Archive new Twitch VODs to GCS + index (uses WORK_DIR, not repo data/)",
    )
    p_night.add_argument("--limit", type=int, default=5)
    p_night.add_argument("--work-dir", type=Path, default=None)
    p_night.add_argument("--force", action="store_true", help="Re-process even if in GCS")
    p_night.add_argument("--dry-run", action="store_true")
    p_night.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete WORK_DIR copy after upload",
    )
    p_night.set_defaults(func=cmd_nightly)

    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
