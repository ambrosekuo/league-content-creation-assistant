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
from datetime import datetime, timezone
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


def _write_archive_manifest(dataset_dir: Path, vod_id: str) -> Path:
    """Write archive_manifest.json summarizing events + clips for GCS."""
    from datetime import datetime, timezone

    events_path = dataset_dir / "lol_events.json"
    clips_subdir = "lol_clips"
    marker = dataset_dir / ".cut_run.json"
    if marker.is_file():
        try:
            clips_subdir = str(
                json.loads(marker.read_text(encoding="utf-8")).get("clips_subdir")
                or "lol_clips"
            )
        except (OSError, json.JSONDecodeError):
            pass
    clips_manifest = dataset_dir / clips_subdir / "clips.json"
    meta_path = dataset_dir / "metadata.json"

    events_payload: dict[str, Any] = {}
    if events_path.is_file():
        events_payload = json.loads(events_path.read_text(encoding="utf-8"))
    matches = events_payload.get("matches") or []
    event_count = sum(len(m.get("events") or []) for m in matches)

    clip_count = 0
    clip_types: list[str] = []
    if clips_manifest.is_file():
        clips_payload = json.loads(clips_manifest.read_text(encoding="utf-8"))
        clip_count = int(clips_payload.get("clip_count") or len(clips_payload.get("clips") or []))
        clip_types = list(clips_payload.get("types") or [])

    twitch_meta: dict[str, Any] = {}
    if meta_path.is_file():
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        twitch_meta = {
            "title": raw.get("title"),
            "timestamp": raw.get("timestamp"),
            "duration": raw.get("duration"),
            "uploader": raw.get("uploader") or raw.get("uploader_id"),
        }

    import storage_gcs as gcs

    bucket = gcs.bucket_name()
    prefix = gcs.vod_prefix(vod_id, dataset_dir=dataset_dir)
    manifest = {
        "schema_version": 1,
        "vodId": vod_id,
        "generatedAt": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "twitch": twitch_meta,
        "player": events_payload.get("player"),
        "matchCount": len(matches),
        "eventCount": event_count,
        "matches": [
            {
                "matchId": m.get("matchId"),
                "champion": m.get("champion"),
                "win": m.get("win"),
                "kills": m.get("kills"),
                "deaths": m.get("deaths"),
                "assists": m.get("assists"),
                "eventCount": len(m.get("events") or []),
            }
            for m in matches
        ],
        "clips": {
            "count": clip_count,
            "types": clip_types,
            "manifestUri": f"gs://{bucket}/{prefix}/lol_clips/clips.json",
            "prefixUri": f"gs://{bucket}/{prefix}/lol_clips/",
        },
        "uris": {
            "source": f"gs://{bucket}/{prefix}/source.mp4",
            "metadata": f"gs://{bucket}/{prefix}/metadata.json",
            "lolEvents": f"gs://{bucket}/{prefix}/lol_events.json",
            "transcript": f"gs://{bucket}/{prefix}/transcript.json",
            "lolEventsSnapped": f"gs://{bucket}/{prefix}/lol_events_snapped.json",
            "archiveManifest": f"gs://{bucket}/{prefix}/archive_manifest.json",
        },
    }
    out = dataset_dir / "archive_manifest.json"
    out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


def _transcript_snap_enabled() -> bool:
    """Default ON. Set TRANSCRIPT_SNAP=0 to cut raw Riot windows only."""
    return os.environ.get("TRANSCRIPT_SNAP", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _resolve_clips_outdir(
    dataset_dir: Path,
    *,
    versioned: bool,
    resume: bool,
    clips_subdir: str | None,
) -> Path:
    """Pick lol_clips or lol_clips_<timestamp>; resume unfinished run via marker."""
    marker = dataset_dir / ".cut_run.json"
    if clips_subdir:
        out = dataset_dir / clips_subdir
        out.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "clips_subdir": clips_subdir,
                    "complete": False,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return out

    if resume and marker.is_file():
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            if not data.get("complete") and data.get("clips_subdir"):
                prev = dataset_dir / str(data["clips_subdir"])
                if prev.is_dir():
                    print(f"[cut] resume folder {prev.name}", flush=True)
                    return prev
        except (OSError, json.JSONDecodeError):
            pass

    base = dataset_dir / "lol_clips"
    has_clips = base.is_dir() and any(base.rglob("c*.mp4"))
    if versioned and has_clips:
        name = f"lol_clips_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%SZ')}"
        print(f"[cut] existing lol_clips/ → versioned {name}", flush=True)
    else:
        name = "lol_clips"
    out = dataset_dir / name
    out.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "clips_subdir": name,
                "complete": False,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return out


def _mark_cut_complete(dataset_dir: Path, clips_dir: Path) -> None:
    marker = dataset_dir / ".cut_run.json"
    marker.write_text(
        json.dumps(
            {
                "clips_subdir": clips_dir.name,
                "complete": True,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _cut_clips(
    dataset_dir: Path,
    *,
    types: str,
    max_clips: int,
    force: bool,
    from_windows: Path | None = None,
    reencode: bool = False,
    stream_copy: bool = True,
    resume: bool = False,
    versioned: bool = False,
    clips_subdir: str | None = None,
    publish_gcs_vod_id: str | None = None,
    clear_local: bool = True,
) -> Path:
    import shutil

    clips_dir = _resolve_clips_outdir(
        dataset_dir,
        versioned=versioned,
        resume=resume,
        clips_subdir=clips_subdir,
    )
    if clear_local and force and not resume and clips_dir.name == "lol_clips":
        if clips_dir.is_dir():
            shutil.rmtree(clips_dir)
            clips_dir.mkdir(parents=True, exist_ok=True)
            print(f"[cut] cleared {clips_dir}", flush=True)

    # Publish run marker early so a cancel can resume from GCS.
    if publish_gcs_vod_id:
        import storage_gcs as gcs

        marker = dataset_dir / ".cut_run.json"
        if marker.is_file():
            day = gcs.resolve_day_key(publish_gcs_vod_id, dataset_dir)
            base = gcs.vod_prefix(publish_gcs_vod_id, day_key=day)
            gcs.upload_file(
                marker,
                f"{base}/.cut_run.json",
                content_type="application/json",
            )

    cmd = [
        sys.executable,
        str(ROOT / "cut_lol_clips.py"),
        "--dataset-dir",
        str(dataset_dir),
        "--output-dir",
        str(clips_dir),
        "--types",
        types,
    ]
    if from_windows is not None:
        cmd.extend(["--from-windows", str(from_windows)])
    if max_clips > 0:
        cmd.extend(["--max-clips", str(max_clips)])
    if force and not resume:
        cmd.append("--force")
    if resume:
        cmd.append("--resume")
        cmd.append("--force")  # allow rewriting clips.json while skipping mp4s
    if reencode:
        cmd.append("--reencode")
    else:
        cmd.append("--stream-copy")
    if publish_gcs_vod_id:
        cmd.extend(["--publish-gcs-vod-id", publish_gcs_vod_id])
    # Pad matches stitch trim so snap pre/post-roll survives freeze removal.
    from clip_edge_pad import PAD_LEAD_S, PAD_TRAIL_S

    cmd.extend(
        [
            "--pad-lead",
            str(PAD_LEAD_S),
            "--pad-trail",
            str(PAD_TRAIL_S),
        ]
    )
    _run(cmd)
    _mark_cut_complete(dataset_dir, clips_dir)
    return clips_dir


def cmd_recut_clips(args: argparse.Namespace) -> int:
    """
    Re-snap + re-cut from an existing GCS archive (no Twitch download).

    Uses source.mp4 + lol_events.json + transcript.json when present, then
    replaces lol_clips/ + lol_events_snapped.json in the archive.

    --fast: versioned folder, publish each clip to GCS, resume skips;
    cuts default to stream-copy (no encoder). Stitch freeze-detect trims
    keyframe freezes. --reencode for frame-accurate libx264.
    """
    import shutil

    import storage_gcs as gcs

    vod_id = str(args.vod_id).strip().lstrip("v")
    work_dir = Path(os.environ.get("WORK_DIR") or args.work_dir or "/tmp/vod-work")
    work_dir.mkdir(parents=True, exist_ok=True)
    dataset_dir = (Path(args.dataset_dir).resolve() if args.dataset_dir else work_dir / vod_id)
    fast = bool(getattr(args, "fast", False))
    resume = bool(getattr(args, "resume", False) or fast)
    versioned = bool(getattr(args, "versioned", False) or fast)
    # Default / --fast: -c copy (no encoder). --reencode: veryfast + accurate seek.
    reencode = bool(getattr(args, "reencode", False))
    stream_copy = bool(getattr(args, "stream_copy", True)) and not reencode
    publish = bool(getattr(args, "publish_incremental", False) or fast)

    if args.dataset_dir:
        if not dataset_dir.is_dir():
            raise FileNotFoundError(dataset_dir)
        source_origin = "dataset_dir"
    else:
        if not gcs.find_source_checkpoint(vod_id):
            raise FileNotFoundError(
                f"No GCS source.mp4 for {vod_id}; cannot recut without a Twitch re-download"
            )
        # Resume/fast: keep local work dir so we can skip already-cut mp4s.
        if dataset_dir.exists() and args.clean_work and not resume:
            shutil.rmtree(dataset_dir, ignore_errors=True)
        gcs.restore_source_checkpoint(vod_id, dataset_dir)
        source_origin = "gcs_checkpoint"
        if resume or fast:
            restored_subdir = gcs.restore_cut_run_progress(vod_id, dataset_dir)
            if restored_subdir and not getattr(args, "clips_subdir", None):
                args.clips_subdir = restored_subdir

    events_path = dataset_dir / "lol_events.json"
    # Always re-index on recut so overlap/bookend fixes land in GCS
    # (stale lol_events.json would permanently drop games that started before the VOD).
    print(f"[index] {vod_id} (refresh)", flush=True)
    _run(
        [
            sys.executable,
            str(ROOT / "lol-indexer" / "lol_indexer.py"),
            "--vod-dir",
            str(dataset_dir),
            "--output",
            str(events_path),
        ],
        cwd=ROOT / "lol-indexer",
    )

    from_windows: Path | None = None
    if _transcript_snap_enabled():
        from_windows = _snap_clips_with_transcript(dataset_dir, types=args.types)
    else:
        print("[snap] TRANSCRIPT_SNAP disabled; cutting raw Riot windows", flush=True)

    # If GCS already has lol_clips/ and we're versioning, force a timestamp folder
    # even when local work dir is empty (Cloud Run clean restore).
    clips_subdir = getattr(args, "clips_subdir", None)
    if versioned and not clips_subdir:
        base = dataset_dir / "lol_clips"
        local_has = base.is_dir() and any(base.rglob("c*.mp4"))
        remote_has = False
        try:
            day = gcs.resolve_day_key(vod_id, dataset_dir)
            remote_has = gcs.blob_exists(
                f"{gcs.vod_prefix(vod_id, day_key=day)}/lol_clips/clips.json"
            )
        except Exception:
            remote_has = False
        if local_has or remote_has:
            clips_subdir = (
                f"lol_clips_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%SZ')}"
            )
            print(f"[cut] versioned output → {clips_subdir}", flush=True)

    mode = (
        "reencode/veryfast+accurate"
        if reencode
        else ("stream-copy" if stream_copy else "superfast/seek-before")
    )
    print(f"[cut] {vod_id} ({mode}, resume={resume}, versioned={versioned})", flush=True)
    clips_dir = _cut_clips(
        dataset_dir,
        types=args.types,
        max_clips=args.max_clips,
        force=True,
        from_windows=from_windows,
        reencode=reencode,
        stream_copy=stream_copy,
        resume=resume,
        versioned=versioned,
        clips_subdir=clips_subdir,
        publish_gcs_vod_id=vod_id if publish else None,
        clear_local=not resume and not versioned,
    )
    _write_archive_manifest(dataset_dir, vod_id)

    print(f"[upload] sidecars for {vod_id}", flush=True)
    uploaded = gcs.upload_clip_artifacts(
        dataset_dir,
        vod_id=vod_id,
        clips_subdir=clips_dir.name,
        replace_clips_prefix=not publish,
    )
    result = {
        "status": "recut",
        "vodId": vod_id,
        "sourceOrigin": source_origin,
        "uploaded": len(uploaded),
        "transcriptSnap": bool(from_windows),
        "fast": fast,
        "reencode": reencode,
        "streamCopy": stream_copy,
        "resume": resume,
        "clipsDir": str(clips_dir),
        "clipsPrefix": (
            f"gs://{gcs.bucket_name()}/"
            f"{gcs.vod_prefix(vod_id, dataset_dir=dataset_dir)}/{clips_dir.name}/"
        ),
    }
    if args.cleanup and not args.dataset_dir and not resume:
        # Keep work dir when resuming is likely; cleanup only for finished one-shots.
        shutil.rmtree(dataset_dir, ignore_errors=True)
    print(json.dumps(result, indent=2))
    return 0


def cmd_process_clips(args: argparse.Namespace) -> int:
    """
    Post-process existing lol_clips/ in GCS (no Twitch / no source.mp4).

    Current tasks:
      - rank lol_clips and keep the top K per game (default 5)
      - generate lobby card intro (~3s) per game
      - stitch selected clips into lol_compilations/gamNN_*.mp4
    """
    import shutil

    import storage_gcs as gcs

    vod_id = str(args.vod_id).strip().lstrip("v")
    work_dir = Path(os.environ.get("WORK_DIR") or args.work_dir or "/tmp/vod-work")
    work_dir.mkdir(parents=True, exist_ok=True)
    dataset_dir = Path(args.dataset_dir).resolve() if args.dataset_dir else work_dir / vod_id

    clips_subdir = (getattr(args, "clips_subdir", None) or "").strip() or None
    if not clips_subdir and not args.dataset_dir:
        # Prefer the latest completed fast-cut folder from .cut_run.json.
        try:
            day = gcs.resolve_day_key(vod_id, None)
            remote = f"{gcs.vod_prefix(vod_id, day_key=day)}/.cut_run.json"
            if gcs.blob_exists(remote):
                marker = dataset_dir / ".cut_run.json"
                dataset_dir.mkdir(parents=True, exist_ok=True)
                gcs.download_file(remote, marker)
                data = json.loads(marker.read_text(encoding="utf-8"))
                sub = str(data.get("clips_subdir") or "").strip()
                if sub and data.get("complete"):
                    clips_subdir = sub
                    print(f"[restore] using cut_run clips_subdir={clips_subdir}", flush=True)
        except Exception as exc:
            print(f"[restore] cut_run lookup skipped: {exc}", flush=True)
    clips_subdir = clips_subdir or "lol_clips"

    if args.dataset_dir:
        if not (dataset_dir / clips_subdir).is_dir():
            raise FileNotFoundError(f"Missing {clips_subdir} in {dataset_dir}")
        origin = "dataset_dir"
    else:
        if dataset_dir.exists() and args.clean_work:
            shutil.rmtree(dataset_dir, ignore_errors=True)
        print(f"[restore] {clips_subdir}/ for {vod_id}", flush=True)
        gcs.restore_clips_prefix(vod_id, dataset_dir, clips_subdir=clips_subdir)
        origin = "gcs_clips"

    from clip_edge_pad import PAD_LEAD_S, PAD_TRAIL_S

    stitch_cmd = [
        sys.executable,
        str(ROOT / "stitch_game_clips.py"),
        "--dataset-dir",
        str(dataset_dir),
        "--clips-dir",
        str(dataset_dir / clips_subdir),
        "--min-clips",
        str(args.min_clips),
        "--top-k",
        str(int(getattr(args, "top_k", 5))),
        "--force",
    ]
    if getattr(args, "no_rank", False):
        stitch_cmd.append("--no-rank")
    if getattr(args, "detect_freeze", True):
        stitch_cmd.append("--detect-freeze")
    else:
        stitch_cmd += [
            "--no-detect-freeze",
            "--trim-lead",
            str(PAD_LEAD_S),
            "--trim-trail",
            str(PAD_TRAIL_S),
        ]
    if getattr(args, "reencode", False):
        print("[stitch] --reencode ignored: clip job is stream-copy only", flush=True)
    print(
        f"[stitch] {vod_id} from {clips_subdir}/ "
        f"top_k={getattr(args, 'top_k', 5)} (copy-trim, no encode)",
        flush=True,
    )
    _run(stitch_cmd)

    print(f"[upload] compilations for {vod_id}", flush=True)
    uploaded = gcs.upload_compilation_artifacts(dataset_dir, vod_id=vod_id)
    prefix = gcs.vod_prefix(vod_id, dataset_dir=dataset_dir)
    result = {
        "status": "process_clips",
        "vodId": vod_id,
        "origin": origin,
        "uploaded": len(uploaded),
        "topK": int(getattr(args, "top_k", 5)),
        "rank": not bool(getattr(args, "no_rank", False)),
        "compilationsPrefix": f"gs://{gcs.bucket_name()}/{prefix}/lol_compilations/",
        "objects": uploaded,
    }
    if args.cleanup and not args.dataset_dir:
        shutil.rmtree(dataset_dir, ignore_errors=True)
    print(json.dumps(result, indent=2))
    return 0


def cmd_process_daily(args: argparse.Namespace) -> int:
    """Rank every clip from every VOD on a calendar day and stitch one compilation."""
    import shutil

    import storage_gcs as gcs

    day = str(getattr(args, "day_key", "") or "").strip().lower()
    vod_id = str(getattr(args, "vod_id", "") or "").strip().lstrip("v")
    if not day and vod_id:
        try:
            day = gcs.resolve_day_key(vod_id, args.dataset_dir)
        except Exception as exc:
            print(f"[daily] day-key lookup skipped: {exc}", flush=True)
    if not day and args.dataset_dir:
        day = gcs.day_key_from_dataset(args.dataset_dir) or "local"
    if not day:
        raise ValueError("Provide --day-key (e.g. aug12_2026) or --vod-id to derive the day")

    work_dir = Path(os.environ.get("WORK_DIR") or args.work_dir or "/tmp/vod-work")
    staging = Path(args.dataset_dir).resolve() if args.dataset_dir else work_dir / f"daily_{day}"
    origin = "dataset_dir"
    vods: list[str] = []
    if args.dataset_dir:
        if not staging.exists():
            raise FileNotFoundError(f"Missing dataset dir {staging}")
    else:
        if staging.exists() and args.clean_work:
            shutil.rmtree(staging, ignore_errors=True)
        print(f"[daily] restore clips for {day}", flush=True)
        vods = gcs.restore_day_clips(day, staging)
        origin = "gcs_day"
        if not vods:
            raise FileNotFoundError(f"No lol_clips for day {day} in GCS")

    clips_dir = staging / "lol_clips" if (staging / "lol_clips").is_dir() else staging
    out_dir = staging / "lol_compilations_daily"
    top_k = int(getattr(args, "top_k", 12) or 12)
    stitch_cmd = [
        sys.executable,
        str(ROOT / "stitch_game_clips.py"),
        "--dataset-dir",
        str(staging),
        "--clips-dir",
        str(clips_dir),
        "--output-dir",
        str(out_dir),
        "--daily",
        "--top-k",
        str(top_k),
        "--max-per-game",
        str(int(getattr(args, "max_per_game", 3))),
        "--order",
        str(getattr(args, "order", "chrono")),
        "--min-clips",
        "1",
        "--no-lobby",
        "--force",
    ]
    print(f"[daily] stitch top {top_k} across {day}", flush=True)
    _run(stitch_cmd)

    uploaded: dict[str, str] = {}
    daily_prefix = None
    if not args.dataset_dir or getattr(args, "upload", False):
        print(f"[upload] daily compilation for {day}", flush=True)
        uploaded = gcs.upload_daily_artifacts(out_dir, day_key=day)
        daily_prefix = f"gs://{gcs.bucket_name()}/{gcs.prefix()}/{day}/_daily/"

    result = {
        "status": "process_daily",
        "dayKey": day,
        "origin": origin,
        "vods": vods,
        "topK": top_k,
        "maxPerGame": int(getattr(args, "max_per_game", 3)),
        "outputDir": str(out_dir),
        "uploaded": len(uploaded),
        "dailyPrefix": daily_prefix,
        "objects": uploaded,
    }
    if args.cleanup and not args.dataset_dir:
        shutil.rmtree(staging, ignore_errors=True)
    print(json.dumps(result, indent=2))
    return 0


def cmd_process_portraits(args: argparse.Namespace) -> int:
    """
    Render 9:16 portrait versions of existing lol_compilations weaves in GCS.

    Downloads weaves only (no source.mp4 / no Twitch), writes
    lol_compilations_portrait/{stem}_portrait.mp4, publishes each to GCS as it finishes.
    """
    import shutil

    import storage_gcs as gcs

    vod_id = str(args.vod_id).strip().lstrip("v")
    work_dir = Path(os.environ.get("WORK_DIR") or args.work_dir or "/tmp/vod-work")
    work_dir.mkdir(parents=True, exist_ok=True)
    dataset_dir = Path(args.dataset_dir).resolve() if args.dataset_dir else work_dir / vod_id

    if args.dataset_dir:
        comp_dir = dataset_dir / "lol_compilations"
        if not comp_dir.is_dir():
            raise FileNotFoundError(f"Missing lol_compilations/ in {dataset_dir}")
        origin = "dataset_dir"
    else:
        if dataset_dir.exists() and args.clean_work:
            shutil.rmtree(dataset_dir, ignore_errors=True)
        print(f"[restore] lol_compilations/ for {vod_id}", flush=True)
        gcs.restore_compilations_prefix(vod_id, dataset_dir)
        origin = "gcs_compilations"

    comp_dir = dataset_dir / "lol_compilations"
    out_dir = dataset_dir / "lol_compilations_portrait"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Prefer new stitch names (gam01_champ_vs_opp_win.mp4); keep legacy *_weave.mp4.
    candidates = list(comp_dir.glob("gam*.mp4")) + list(comp_dir.glob("*_weave.mp4"))
    weaves = sorted(
        {
            p
            for p in candidates
            if p.is_file()
            and "lobby" not in p.name.lower()
            and not p.name.endswith("_portrait.mp4")
        },
        key=lambda p: p.name,
    )
    if getattr(args, "max_weaves", 0):
        weaves = weaves[: max(0, int(args.max_weaves))]
        print(f"[portrait] limited to first {len(weaves)} weave(s)", flush=True)
    if not weaves:
        raise FileNotFoundError(
            f"No gam*.mp4 or *_weave.mp4 under {comp_dir} "
            "(lobby intros are ignored)"
        )

    # Clear remote once up front when forcing a full re-render (not dataset-dir runs).
    if args.force and not args.dataset_dir:
        day = gcs.resolve_day_key(vod_id, dataset_dir)
        remote_prefix = f"{gcs.vod_prefix(vod_id, day_key=day)}/lol_compilations_portrait/"
        removed = gcs.delete_prefix(remote_prefix)
        if removed:
            print(f"[upload] cleared {removed} old portrait object(s)", flush=True)

    portraits: list[dict[str, Any]] = []
    uploaded: dict[str, str] = {}
    report_path = out_dir / "portraits.json"

    def write_and_publish_report() -> None:
        report = {
            "schema_version": 1,
            "vodId": vod_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "intro": str(args.intro),
            "outro": bool(args.outro),
            "still_seconds": float(args.still_seconds),
            "still_mode": str(args.still_mode),
            "game_mode": str(args.game_mode),
            "kda_overlay": bool(args.kda_overlay),
            "track_champion": bool(args.track_champion),
            "preset": str(args.preset),
            "crf": int(args.crf),
            "portrait_count": len(portraits),
            "portraits": portraits,
        }
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        if not args.dataset_dir:
            uploaded.update(
                gcs.upload_portrait_paths(dataset_dir, [report_path], vod_id=vod_id)
            )

    for weave in weaves:
        out_mp4 = out_dir / f"{weave.stem}_portrait.mp4"
        preview = out_dir / f"{weave.stem}_portrait_preview.jpg"
        if out_mp4.exists() and not args.force:
            print(f"[skip] exists {out_mp4.name} (pass --force)", flush=True)
            portraits.append(
                {
                    "source": weave.name,
                    "output": out_mp4.name,
                    "skipped": True,
                }
            )
            if not args.dataset_dir:
                paths = [out_mp4]
                if preview.is_file():
                    paths.append(preview)
                uploaded.update(
                    gcs.upload_portrait_paths(dataset_dir, paths, vod_id=vod_id)
                )
            write_and_publish_report()
            continue

        cmd = [
            sys.executable,
            str(ROOT / "render_portrait.py"),
            "--input",
            str(weave),
            "--output",
            str(out_mp4),
            "--preview-frame",
            str(preview),
            "--intro",
            str(args.intro),
            "--overlay-hold",
            str(args.overlay_hold),
            "--end-seconds",
            str(args.end_seconds),
            "--game-mode",
            str(args.game_mode),
            "--game-zoom",
            str(args.game_zoom),
            "--cam-hole",
            str(args.cam_hole),
            "--preset",
            str(args.preset),
            "--crf",
            str(args.crf),
        ]
        if args.outro:
            cmd.append("--outro")
        else:
            cmd.append("--no-outro")
        if str(args.intro) == "story":
            still = float(args.still_seconds) if float(args.still_seconds) > 0 else 3.0
            cmd += [
                "--still-seconds",
                str(still),
                "--still-mode",
                str(args.still_mode),
            ]
        if args.kda_overlay:
            cmd.append("--kda-overlay")
        else:
            cmd.append("--no-kda-overlay")
        lobby_png = weave.with_name(f"{weave.stem}_lobby.png")
        lobby_meta = weave.with_name(f"{weave.stem}_lobby_meta.json")
        if lobby_png.is_file():
            cmd += ["--lobby-png", str(lobby_png)]
        if lobby_meta.is_file():
            cmd += ["--lobby-meta", str(lobby_meta)]
        if args.track_champion:
            cmd += [
                "--track-champion",
                "--track-fps",
                str(args.track_fps),
                "--track-dead-zone",
                str(args.track_dead_zone),
                "--track-ease-ms",
                str(args.track_ease_ms),
                "--track-max-speed",
                str(args.track_max_speed),
                "--track-self-bias",
                str(args.track_self_bias),
                "--track-enemy-pull",
                str(args.track_enemy_pull),
                "--track-pan-cooldown",
                str(args.track_pan_cooldown),
                "--track-outside-hold",
                str(args.track_outside_hold),
            ]
        cmd += [
            "--music",
            str(args.music),
            "--music-db",
            str(args.music_db),
        ]
        print(
            f"[portrait] {weave.name} → {out_mp4.name} "
            f"(intro={args.intro} outro={'on' if args.outro else 'off'} "
            f"zoom={args.game_zoom} "
            f"track={'on' if args.track_champion else 'off'} "
            f"music={args.music} "
            f"preset={args.preset} crf={args.crf})",
            flush=True,
        )
        _run(cmd)
        portraits.append(
            {
                "source": weave.name,
                "output": out_mp4.name,
                "preview": preview.name if preview.is_file() else None,
                "skipped": False,
            }
        )
        if not args.dataset_dir:
            paths = [out_mp4]
            if preview.is_file():
                paths.append(preview)
            track_json = out_dir / f"{weave.stem}_portrait_track.json"
            if track_json.is_file():
                paths.append(track_json)
            uploaded.update(
                gcs.upload_portrait_paths(dataset_dir, paths, vod_id=vod_id)
            )
        write_and_publish_report()

    prefix = gcs.vod_prefix(vod_id, dataset_dir=dataset_dir)
    result = {
        "status": "process_portraits",
        "vodId": vod_id,
        "origin": origin,
        "uploaded": len(uploaded),
        "portraitsPrefix": f"gs://{gcs.bucket_name()}/{prefix}/lol_compilations_portrait/",
        "portrait_count": len(portraits),
        "portraits": [p.get("output") for p in portraits],
        "objects": uploaded,
        "preset": str(args.preset),
        "crf": int(args.crf),
        "track_champion": bool(args.track_champion),
    }
    if args.cleanup and not args.dataset_dir:
        shutil.rmtree(dataset_dir, ignore_errors=True)
    print(json.dumps(result, indent=2))
    return 0


def _snap_clips_with_transcript(dataset_dir: Path, *, types: str) -> Path:
    """Event-window ASR (if needed) → center-on-event snap → lol_events_snapped.json."""
    allowed = {"KILL", "DEATH", "ASSIST"}
    snap_types = ",".join(
        t for t in (x.strip().upper() for x in types.split(",")) if t in allowed
    ) or "KILL,DEATH,ASSIST"

    transcript_path = dataset_dir / "transcript.json"
    if transcript_path.is_file():
        print(f"[transcribe-windows] reuse {transcript_path.name}", flush=True)
    else:
        # Wider ASR context than the cut window — captions/nudge still benefit.
        print(f"[transcribe-windows] {dataset_dir.name}", flush=True)
        _run(
            [
                sys.executable,
                str(ROOT / "transcribe_event_windows.py"),
                "--dataset-dir",
                str(dataset_dir),
                "--types",
                snap_types,
                "--pre-roll",
                "40",
                "--post-roll",
                "30",
            ]
        )

    snapped = dataset_dir / "lol_events_snapped.json"
    print(
        f"[snap] {dataset_dir.name} (center-on-event ~8s+10s, max 22s, overlap-merge)",
        flush=True,
    )
    _run(
        [
            sys.executable,
            str(ROOT / "snap_clips_to_transcript.py"),
            "--dataset-dir",
            str(dataset_dir),
            "--types",
            snap_types,
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
            "--overlap-slack",
            "1",
            "--game-end-duration",
            "13",
            "--game-end-tail",
            "5",
            "--output",
            str(snapped),
        ]
    )
    return snapped


def _finalize_and_upload(
    dataset_dir: Path,
    vod_id: str,
    *,
    cut_clips: bool,
    clip_types: str,
    max_clips: int,
    fast: bool = False,
    skip_source_upload: bool = False,
) -> dict[str, Any]:
    import storage_gcs as gcs

    events_out = dataset_dir / "lol_events.json"
    if not events_out.is_file():
        print(f"[index] {vod_id}", flush=True)
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

    from_windows: Path | None = None
    if cut_clips and _transcript_snap_enabled():
        try:
            from_windows = _snap_clips_with_transcript(dataset_dir, types=clip_types)
        except Exception as exc:
            # Don't lose the archive if ASR fails — fall back to raw Riot cuts.
            print(f"[snap] failed, falling back to raw cuts: {exc}", flush=True)
            from_windows = None
    elif cut_clips and not _transcript_snap_enabled():
        print("[snap] TRANSCRIPT_SNAP disabled; cutting raw Riot windows", flush=True)

    # Default: -c copy (no encoder). Stitch freeze-detect drops keyframe freezes.
    reencode = False
    resume = fast
    publish = fast
    # Partial / re-process runs: keep existing lol_clips/ in GCS untouched.
    versioned = False
    clips_subdir: str | None = None
    if fast:
        try:
            day = gcs.resolve_day_key(vod_id, dataset_dir)
            remote_has = gcs.blob_exists(
                f"{gcs.vod_prefix(vod_id, day_key=day)}/lol_clips/clips.json"
            )
        except Exception:
            remote_has = False
        if remote_has or (dataset_dir / "lol_clips").is_dir():
            versioned = True
            clips_subdir = (
                f"lol_clips_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%SZ')}"
            )
            print(f"[cut] versioned output → {clips_subdir}", flush=True)

    clips_dir: Path | None = None
    if cut_clips:
        mode = "stream-copy"
        print(f"[cut] {vod_id} ({mode})", flush=True)
        clips_dir = _cut_clips(
            dataset_dir,
            types=clip_types,
            max_clips=max_clips,
            force=True,
            from_windows=from_windows,
            reencode=reencode,
            stream_copy=True,
            resume=resume,
            versioned=versioned,
            clips_subdir=clips_subdir,
            publish_gcs_vod_id=vod_id if publish else None,
            clear_local=not resume and not versioned,
        )

    print(f"[manifest] {vod_id}", flush=True)
    _write_archive_manifest(dataset_dir, vod_id)

    print(f"[upload] {vod_id}", flush=True)
    clips_name = clips_dir.name if clips_dir is not None else "lol_clips"
    if skip_source_upload or (fast and clips_dir is not None):
        # Never overwrite the full archive source.mp4 after a section/fast cut.
        uploaded = gcs.upload_clip_artifacts(
            dataset_dir,
            vod_id=vod_id,
            clips_subdir=clips_name,
            replace_clips_prefix=not publish,
        )
    else:
        uploaded = gcs.upload_dataset_dir(dataset_dir, vod_id=vod_id)
    return {
        "status": "archived",
        "vodId": vod_id,
        "uploaded": len(uploaded),
        "clipsDir": str(dataset_dir / clips_name),
        "transcriptSnap": bool(from_windows),
        "fast": fast,
        "reencode": reencode if cut_clips else None,
        "skipSourceUpload": skip_source_upload,
        "archiveManifest": f"gs://{gcs.bucket_name()}/{gcs.vod_prefix(vod_id)}/archive_manifest.json",
        "clipsPrefix": f"gs://{gcs.bucket_name()}/{gcs.vod_prefix(vod_id)}/{clips_name}/",
    }


def _local_source_ok(dataset_dir: Path) -> bool:
    for path in dataset_dir.glob("source.*"):
        if path.is_file() and path.suffix.lower() in {".mp4", ".mkv", ".mov", ".webm"}:
            if ".part" in path.name:
                continue
            if path.stat().st_size >= 50 * 1024 * 1024:
                return True
    return False


def _ensure_source(
    vod_id: str,
    dataset_dir: Path,
    *,
    work_dir: Path,
    url: str,
    force_redownload: bool,
    section_start: float | None = None,
    section_end: float | None = None,
) -> str:
    """
    Make sure dataset_dir has source media.

    Order:
      1) local source already present
      2) resume from GCS checkpoint (vods/ or work/) — no Twitch
      3) download from Twitch, then immediately checkpoint to GCS

    Section downloads never overwrite the canonical vods/.../source.mp4
    (partial files stay local / optional work/ only).
    """
    import storage_gcs as gcs

    dataset_dir.mkdir(parents=True, exist_ok=True)
    partial = section_start is not None and section_end is not None

    if not force_redownload and _local_source_ok(dataset_dir):
        print(f"[source] using local files in {dataset_dir}", flush=True)
        return "local"

    if not force_redownload and not partial and gcs.find_source_checkpoint(vod_id):
        gcs.restore_source_checkpoint(vod_id, dataset_dir)
        return "gcs_checkpoint"

    if force_redownload and dataset_dir.exists():
        import shutil

        # Drop stale local media so yt-dlp writes a clean source.mp4.
        for path in dataset_dir.glob("source.*"):
            path.unlink(missing_ok=True)

    print(f"[ingest] Twitch download {vod_id} → {dataset_dir}", flush=True)
    # Cap quality in cloud: full "best" ~32GiB/12h blows Cloud Run /tmp (SIGBUS).
    fmt = os.environ.get(
        "YTDLP_FORMAT",
        "best[height<=1080]/best[height<=1080]/best",
    )
    cmd = [
        sys.executable,
        str(ROOT / "ingest_vod.py"),
        url,
        "--id",
        vod_id,
        "--output-root",
        str(work_dir),
        "--format",
        fmt,
        "--skip-audio",
    ]
    if partial:
        cmd.extend(
            [
                "--section-start",
                str(section_start),
                "--section-end",
                str(section_end),
            ]
        )
        print(
            f"[ingest] section {section_start:.0f}s–{section_end:.0f}s "
            f"(will not overwrite GCS source.mp4)",
            flush=True,
        )
    _run(cmd)
    if not partial:
        print(f"[checkpoint] uploading source to GCS for resume", flush=True)
        gcs.checkpoint_source_files(dataset_dir, vod_id)
    else:
        print("[checkpoint] skipped for section download (protect full archive)", flush=True)
    return "twitch_section" if partial else "twitch"


def _restore_sidecar_if_missing(vod_id: str, dataset_dir: Path, name: str) -> bool:
    """Pull a sidecar from the existing GCS archive when local copy is missing."""
    import storage_gcs as gcs

    dest = dataset_dir / name
    if dest.is_file():
        return True
    day = gcs.resolve_day_key(vod_id, dataset_dir)
    for base in (
        gcs.vod_prefix(vod_id, day_key=day),
        gcs.legacy_vod_prefix(vod_id),
    ):
        remote = f"{base}/{name}"
        if gcs.blob_exists(remote):
            print(f"[resume] sidecar {remote}", flush=True)
            gcs.download_file(remote, dest)
            return True
    return False


def cmd_process_vod(args: argparse.Namespace) -> int:
    """Ingest (or resume) + index + cut clips + upload one VOD to GCS."""
    import storage_gcs as gcs

    vod_id = str(args.vod_id).strip().lstrip("v")
    if gcs.vod_archived(vod_id) and not args.force:
        print(json.dumps({"status": "skipped_already_archived", "vodId": vod_id}, indent=2))
        return 0

    section_start = getattr(args, "section_start", None)
    section_end = getattr(args, "section_end", None)
    if (section_start is None) ^ (section_end is None):
        raise SystemExit("Provide both --section-start and --section-end (or neither).")
    partial = section_start is not None and section_end is not None

    if args.dataset_dir:
        dataset_dir = Path(args.dataset_dir).resolve()
        if not dataset_dir.is_dir():
            raise FileNotFoundError(dataset_dir)
        source_origin = "dataset_dir"
    else:
        # IMPORTANT: download to local disk (/tmp), not GCS FUSE.
        # yt-dlp HLS fragment writes are unreliable through FUSE.
        work_dir = Path(os.environ.get("WORK_DIR") or args.work_dir or "/tmp/vod-work")
        work_dir.mkdir(parents=True, exist_ok=True)
        dataset_dir = work_dir / vod_id
        url = args.url or f"https://www.twitch.tv/videos/{vod_id}"
        source_origin = _ensure_source(
            vod_id,
            dataset_dir,
            work_dir=work_dir,
            url=url,
            force_redownload=args.force_redownload or partial,
            section_start=section_start,
            section_end=section_end,
        )

    # Reuse full-VOD transcript for snap on section runs (offsets still valid).
    if _transcript_snap_enabled():
        _restore_sidecar_if_missing(vod_id, dataset_dir, "transcript.json")

    result = _finalize_and_upload(
        dataset_dir,
        vod_id,
        cut_clips=not args.skip_clips,
        clip_types=args.types,
        max_clips=args.max_clips,
        fast=bool(getattr(args, "fast", False)),
        skip_source_upload=partial,
    )
    result["sourceOrigin"] = source_origin
    if partial:
        result["section"] = {"start": section_start, "end": section_end}
    # Cleanup only deletes the ephemeral local copy. GCS checkpoint remains.
    # Keep work dir on --fast so a cancelled cut can resume without re-download.
    if args.cleanup and not args.dataset_dir and not getattr(args, "fast", False):
        import shutil

        shutil.rmtree(dataset_dir, ignore_errors=True)
    print(json.dumps(result, indent=2))
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
        source_origin = _ensure_source(
            vod_id,
            dataset_dir,
            work_dir=work_dir,
            url=url,
            force_redownload=args.force_redownload,
        )

        finalized = _finalize_and_upload(
            dataset_dir,
            vod_id,
            cut_clips=args.cut_clips,
            clip_types=args.types,
            max_clips=args.max_clips,
        )
        finalized["sourceOrigin"] = source_origin
        entry.update(finalized)
        results.append(entry)

        if args.cleanup:
            import shutil

            # Local /tmp only — GCS vods/ + work/ checkpoints are kept.
            shutil.rmtree(dataset_dir, ignore_errors=True)

    summary = {
        "channel": channel,
        "workDir": str(work_dir),
        "results": results,
    }
    print(json.dumps(summary, indent=2))
    return 0


def _build_segments(
    duration: float, *, seg_len: float, pad: float
) -> list[dict[str, Any]]:
    if duration <= 0:
        raise ValueError("VOD duration must be positive")
    if seg_len <= 0:
        raise ValueError("segment length must be positive")
    segments: list[dict[str, Any]] = []
    index = 0
    core = 0.0
    while core < duration:
        core_end = min(duration, core + seg_len)
        download_start = max(0.0, core - (pad if index > 0 else 0.0))
        download_end = min(duration, core_end + pad)
        segments.append(
            {
                "index": index,
                "id": f"{index:02d}",
                "coreStart": round(core, 3),
                "coreEnd": round(core_end, 3),
                "start": round(download_start, 3),
                "end": round(download_end, 3),
            }
        )
        index += 1
        core = core_end
        if core_end >= duration:
            break
    return segments


def _filter_events_for_core(
    events_payload: dict[str, Any],
    core_start: float,
    core_end: float,
    *,
    is_last: bool = False,
) -> dict[str, Any]:
    """Keep events whose vodOffset falls in [core_start, core_end) (inclusive end if last)."""
    out = dict(events_payload)
    matches_out: list[dict[str, Any]] = []
    for match in events_payload.get("matches") or []:
        kept = []
        for event in match.get("events") or []:
            off = event.get("vodOffsetSeconds")
            if off is None:
                continue
            t = float(off)
            if t < core_start:
                continue
            if is_last:
                if t > core_end:
                    continue
            elif t >= core_end:
                continue
            kept.append(event)
        if kept:
            m = dict(match)
            m["events"] = kept
            matches_out.append(m)
    out["matches"] = matches_out
    return out


def _segment_gcs_prefix(vod_id: str, day_key: str, seg_id: str) -> str:
    import storage_gcs as gcs

    return f"{gcs.prefix()}/{day_key}/{vod_id.strip().lstrip('v')}/segments/{seg_id}"


def _segment_done(vod_id: str, day_key: str, seg_id: str) -> bool:
    import storage_gcs as gcs

    return gcs.blob_exists(f"{_segment_gcs_prefix(vod_id, day_key, seg_id)}/segment_manifest.json")


def cmd_process_segmented(args: argparse.Namespace) -> int:
    """
    Plan a long VOD into time segments, process each independently.

    Per segment: download section → event ASR → snap → cut → upload to
    vods/{day}/{id}/segments/{NN}/ so clips appear incrementally and retries
    skip finished slices.
    """
    import storage_gcs as gcs
    from ingest_vod import fetch_vod_metadata

    vod_id = str(args.vod_id).strip().lstrip("v")
    url = args.url or f"https://www.twitch.tv/videos/{vod_id}"
    work_root = Path(os.environ.get("WORK_DIR") or args.work_dir or "/tmp/vod-work")
    work_root.mkdir(parents=True, exist_ok=True)
    dataset_dir = work_root / vod_id
    dataset_dir.mkdir(parents=True, exist_ok=True)

    seg_len = float(os.environ.get("SEGMENT_SECONDS") or args.segment_seconds)
    pad = float(os.environ.get("SEGMENT_PAD_SECONDS") or args.segment_pad)

    # Optional Cloud Run multi-task: only process one segment index.
    task_index = os.environ.get("CLOUD_RUN_TASK_INDEX")
    task_count = os.environ.get("CLOUD_RUN_TASK_COUNT")

    print(f"[segmented] vod={vod_id} seg_len={seg_len}s pad={pad}s", flush=True)

    # --- metadata + plan ---
    meta_path = dataset_dir / "metadata.json"
    if not meta_path.is_file():
        print("[segmented] fetching metadata", flush=True)
        meta = fetch_vod_metadata(url)
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    else:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))

    duration = float(meta.get("duration") or 0)
    if duration <= 0:
        raise RuntimeError("metadata missing duration")

    day_key = gcs.resolve_day_key(vod_id, dataset_dir)
    plan_path = dataset_dir / "plan.json"
    segments = _build_segments(duration, seg_len=seg_len, pad=pad)
    plan = {
        "schema_version": 1,
        "vodId": vod_id,
        "dayKey": day_key,
        "durationSeconds": duration,
        "segmentSeconds": seg_len,
        "padSeconds": pad,
        "segmentCount": len(segments),
        "segments": segments,
        "title": meta.get("title"),
        "url": url,
    }
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    gcs.upload_file(plan_path, f"{gcs.vod_prefix(vod_id, day_key=day_key)}/plan.json", content_type="application/json")
    gcs.upload_file(meta_path, f"{gcs.vod_prefix(vod_id, day_key=day_key)}/metadata.json", content_type="application/json")
    print(f"[segmented] plan {len(segments)} segments → day={day_key}", flush=True)

    # --- Riot index once (metadata-only dir is enough) ---
    events_path = dataset_dir / "lol_events.json"
    if not events_path.is_file():
        print("[segmented] indexing Riot events", flush=True)
        _run(
            [
                sys.executable,
                str(ROOT / "lol-indexer" / "lol_indexer.py"),
                "--vod-dir",
                str(dataset_dir),
                "--output",
                str(events_path),
            ],
            cwd=ROOT / "lol-indexer",
        )
    gcs.upload_file(
        events_path,
        f"{gcs.vod_prefix(vod_id, day_key=day_key)}/lol_events.json",
        content_type="application/json",
    )
    events_payload = json.loads(events_path.read_text(encoding="utf-8"))

    # Which segments this task owns
    to_run = list(segments)
    if getattr(args, "max_segments", 0) and args.max_segments > 0:
        to_run = to_run[: int(args.max_segments)]
        print(f"[segmented] max_segments={args.max_segments} → running {len(to_run)}", flush=True)
    if task_index is not None and task_count is not None:
        ti, tc = int(task_index), int(task_count)
        to_run = [s for s in to_run if int(s["index"]) % tc == ti]
        print(f"[segmented] task {ti}/{tc} owns {len(to_run)} segment(s)", flush=True)

    fmt = os.environ.get(
        "YTDLP_FORMAT",
        "best[height<=1080]/best[height<=1080]/best",
    )
    results: list[dict[str, Any]] = []

    for seg in to_run:
        seg_id = str(seg["id"])
        if _segment_done(vod_id, day_key, seg_id) and not args.force:
            print(f"[segmented] skip {seg_id} (already in GCS)", flush=True)
            results.append({"segment": seg_id, "status": "skipped_done"})
            continue

        seg_dir = dataset_dir / f"seg_{seg_id}"
        if seg_dir.exists():
            import shutil

            shutil.rmtree(seg_dir, ignore_errors=True)
        seg_dir.mkdir(parents=True, exist_ok=True)

        is_last = int(seg["index"]) >= len(segments) - 1
        (seg_dir / "metadata.json").write_bytes(meta_path.read_bytes())
        filtered = _filter_events_for_core(
            events_payload,
            float(seg["coreStart"]),
            float(seg["coreEnd"]),
            is_last=is_last,
        )
        event_count = sum(len(m.get("events") or []) for m in filtered.get("matches") or [])
        (seg_dir / "lol_events.json").write_text(
            json.dumps(filtered, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (seg_dir / "section.json").write_text(json.dumps(seg, indent=2) + "\n", encoding="utf-8")

        print(
            f"[segmented] === seg {seg_id} {seg['start']:.0f}-{seg['end']:.0f}s "
            f"({event_count} events) ===",
            flush=True,
        )

        if event_count == 0 and not args.keep_empty_segments:
            manifest = {
                "vodId": vod_id,
                "segment": seg,
                "dayKey": day_key,
                "clipCount": 0,
                "eventCount": 0,
                "status": "empty",
            }
            local_man = seg_dir / "segment_manifest.json"
            local_man.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            gcs.upload_file(
                local_man,
                f"{_segment_gcs_prefix(vod_id, day_key, seg_id)}/segment_manifest.json",
                content_type="application/json",
            )
            results.append({"segment": seg_id, "status": "empty"})
            continue

        print(f"[segmented] download section {seg_id}", flush=True)
        _run(
            [
                sys.executable,
                str(ROOT / "ingest_vod.py"),
                url,
                "--id",
                "media",
                "--output-root",
                str(seg_dir),
                "--format",
                fmt,
                "--skip-audio",
                "--section-start",
                str(seg["start"]),
                "--section-end",
                str(seg["end"]),
                "--force",
            ]
        )
        media_dir = seg_dir / "media"
        if media_dir.is_dir():
            for path in media_dir.iterdir():
                if path.is_file():
                    path.replace(seg_dir / path.name)
            try:
                media_dir.rmdir()
            except OSError:
                pass

        # Keep full-VOD timeline metadata for indexer offsets / day key.
        (seg_dir / "metadata.json").write_bytes(meta_path.read_bytes())
        (seg_dir / "lol_events.json").write_text(
            json.dumps(filtered, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        from_windows: Path | None = None
        if _transcript_snap_enabled() and event_count > 0:
            try:
                print(f"[segmented] transcribe+snap {seg_id}", flush=True)
                _run(
                    [
                        sys.executable,
                        str(ROOT / "transcribe_event_windows.py"),
                        "--dataset-dir",
                        str(seg_dir),
                        "--types",
                        "KILL,DEATH",
                        "--section-start",
                        str(seg["coreStart"]),
                        "--section-end",
                        str(seg["coreEnd"]),
                        "--timeline-offset",
                        str(seg["start"]),
                        "--force",
                    ]
                )
                snapped = seg_dir / "lol_events_snapped.json"
                _run(
                    [
                        sys.executable,
                        str(ROOT / "snap_clips_to_transcript.py"),
                        "--dataset-dir",
                        str(seg_dir),
                        "--types",
                        "KILL,DEATH",
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
                    ]
                )
                from_windows = snapped
            except Exception as exc:
                print(f"[segmented] snap failed seg {seg_id}: {exc}", flush=True)

        clips_dir = seg_dir / "lol_clips"
        if event_count > 0:
            print(f"[segmented] cut {seg_id}", flush=True)
            from clip_edge_pad import PAD_LEAD_S, PAD_TRAIL_S

            cut_cmd = [
                sys.executable,
                str(ROOT / "cut_lol_clips.py"),
                "--dataset-dir",
                str(seg_dir),
                "--types",
                args.types,
                "--timeline-offset",
                str(seg["start"]),
                "--section-start",
                str(seg["coreStart"]),
                "--section-end",
                str(seg["coreEnd"]),
                "--force",
                "--pad-lead",
                str(PAD_LEAD_S),
                "--pad-trail",
                str(PAD_TRAIL_S),
            ]
            if getattr(args, "reencode", False):
                cut_cmd.append("--reencode")
            else:
                cut_cmd.append("--stream-copy")
            if from_windows is not None:
                cut_cmd.extend(["--from-windows", str(from_windows)])
            if args.max_clips > 0:
                cut_cmd.extend(["--max-clips", str(args.max_clips)])
            _run(cut_cmd)

        clip_count = 0
        clips_json = clips_dir / "clips.json"
        if clips_json.is_file():
            clip_count = int(json.loads(clips_json.read_text()).get("clip_count") or 0)

        # Upload segment artifacts
        seg_prefix = _segment_gcs_prefix(vod_id, day_key, seg_id)
        uploaded = 0
        for path in sorted(seg_dir.rglob("*")):
            if not path.is_file():
                continue
            if ".part" in path.name or path.suffix == ".ytdl":
                continue
            # Skip huge section source from canonical segment upload? Keep it for resume.
            rel = path.relative_to(seg_dir).as_posix()
            ctype = "application/json" if path.suffix == ".json" else (
                "video/mp4" if path.suffix == ".mp4" else None
            )
            gcs.upload_file(path, f"{seg_prefix}/{rel}", content_type=ctype)
            uploaded += 1

        # Also mirror clips into flat lol_clips/ for easier browsing
        if clips_dir.is_dir():
            for path in sorted(clips_dir.rglob("*.mp4")):
                rel = path.relative_to(clips_dir).as_posix()
                gcs.upload_file(
                    path,
                    f"{gcs.vod_prefix(vod_id, day_key=day_key)}/lol_clips/seg{seg_id}/{rel}",
                    content_type="video/mp4",
                )

        seg_manifest = {
            "vodId": vod_id,
            "dayKey": day_key,
            "segment": seg,
            "eventCount": event_count,
            "clipCount": clip_count,
            "uploaded": uploaded,
            "prefix": f"gs://{gcs.bucket_name()}/{seg_prefix}/",
            "status": "ok",
        }
        local_man = seg_dir / "segment_manifest.json"
        local_man.write_text(json.dumps(seg_manifest, indent=2) + "\n", encoding="utf-8")
        gcs.upload_file(
            local_man,
            f"{seg_prefix}/segment_manifest.json",
            content_type="application/json",
        )
        print(f"[segmented] done {seg_id} clips={clip_count} → {seg_manifest['prefix']}", flush=True)
        results.append({"segment": seg_id, "status": "ok", "clipCount": clip_count})

        if args.cleanup:
            import shutil

            shutil.rmtree(seg_dir, ignore_errors=True)

    # Only mark fully archived when every planned segment is done (not a --max-segments smoke test).
    done = sum(1 for s in segments if _segment_done(vod_id, day_key, str(s["id"])))
    if done >= len(segments):
        print("[segmented] all segments complete — writing archive_manifest", flush=True)
        archive = {
            "schema_version": 1,
            "vodId": vod_id,
            "dayKey": day_key,
            "mode": "segmented",
            "segmentCount": len(segments),
            "segmentsCompleted": done,
            "durationSeconds": duration,
            "segments": [
                {
                    "id": s["id"],
                    "uri": f"gs://{gcs.bucket_name()}/{_segment_gcs_prefix(vod_id, day_key, s['id'])}/",
                }
                for s in segments
            ],
            "uris": {
                "plan": f"gs://{gcs.bucket_name()}/{gcs.vod_prefix(vod_id, day_key=day_key)}/plan.json",
                "lolEvents": f"gs://{gcs.bucket_name()}/{gcs.vod_prefix(vod_id, day_key=day_key)}/lol_events.json",
                "clipsPrefix": f"gs://{gcs.bucket_name()}/{gcs.vod_prefix(vod_id, day_key=day_key)}/lol_clips/",
            },
        }
        arch_path = dataset_dir / "archive_manifest.json"
        arch_path.write_text(json.dumps(archive, indent=2) + "\n", encoding="utf-8")
        gcs.upload_file(
            arch_path,
            f"{gcs.vod_prefix(vod_id, day_key=day_key)}/archive_manifest.json",
            content_type="application/json",
        )

    print(
        json.dumps(
            {
                "status": "segmented",
                "vodId": vod_id,
                "dayKey": day_key,
                "segmentsDone": done,
                "segmentCount": len(segments),
                "results": results,
                "prefix": f"gs://{gcs.bucket_name()}/{gcs.vod_prefix(vod_id, day_key=day_key)}/",
            },
            indent=2,
        )
    )
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

    p_one = sub.add_parser(
        "process-vod",
        help="Index + cut clips + upload one VOD (ingest unless --dataset-dir)",
    )
    p_one.add_argument("--vod-id", required=True)
    p_one.add_argument("--url", default=None, help="Override Twitch VOD URL")
    p_one.add_argument(
        "--dataset-dir",
        type=Path,
        default=None,
        help="Use existing local dataset (skip Twitch download)",
    )
    p_one.add_argument("--work-dir", type=Path, default=None)
    p_one.add_argument(
        "--force",
        action="store_true",
        help="Re-run even if archive_manifest.json already exists in GCS",
    )
    p_one.add_argument(
        "--force-redownload",
        action="store_true",
        help="Ignore GCS/local source checkpoints and download from Twitch again",
    )
    p_one.add_argument("--skip-clips", action="store_true")
    p_one.add_argument("--types", default="KILL,DEATH,ASSIST")
    p_one.add_argument("--max-clips", type=int, default=0)
    p_one.add_argument(
        "--fast",
        action="store_true",
        help=(
            "Incremental GCS publish + resume. Cuts are stream-copy (no encoder)."
        ),
    )
    p_one.add_argument(
        "--section-start",
        type=float,
        default=None,
        help="Optional yt-dlp section start (seconds). Use with --section-end for one-game tests.",
    )
    p_one.add_argument(
        "--section-end",
        type=float,
        default=None,
        help="Optional yt-dlp section end (seconds, exclusive of full VOD).",
    )
    p_one.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete local WORK_DIR copy after success (GCS checkpoint kept)",
    )
    p_one.set_defaults(func=cmd_process_vod)

    p_recut = sub.add_parser(
        "recut-clips",
        help=(
            "Re-snap + re-cut clips from an existing GCS archive "
            "(no Twitch download; reuses source/transcript)"
        ),
    )
    p_recut.add_argument("--vod-id", required=True)
    p_recut.add_argument(
        "--dataset-dir",
        type=Path,
        default=None,
        help="Use existing local dataset (skip GCS source restore)",
    )
    p_recut.add_argument("--work-dir", type=Path, default=None)
    p_recut.add_argument("--types", default="KILL,DEATH,ASSIST")
    p_recut.add_argument("--max-clips", type=int, default=0)
    p_recut.add_argument(
        "--fast",
        action="store_true",
        help=(
            "Versioned lol_clips_<timestamp> if lol_clips exists, publish each "
            "clip to GCS immediately, resume skips. Cuts are stream-copy "
            "(no encoder) unless --reencode."
        ),
    )
    p_recut.add_argument(
        "--reencode",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "libx264 veryfast + frame-accurate seek after -i (slow on long VODs). "
            "Default is stream-copy (no encoder)."
        ),
    )
    p_recut.add_argument(
        "--stream-copy",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="ffmpeg -c copy when cutting (default: on). Encoding is opt-in via --reencode.",
    )
    p_recut.add_argument(
        "--resume",
        action="store_true",
        help="Continue an incomplete cut run (skip existing mp4s; implied by --fast)",
    )
    p_recut.add_argument(
        "--versioned",
        action="store_true",
        help="Write to lol_clips_<timestamp> when lol_clips already has clips",
    )
    p_recut.add_argument(
        "--clips-subdir",
        default=None,
        help="Explicit clips folder name under the dataset (e.g. lol_clips_20260811_191500Z)",
    )
    p_recut.add_argument(
        "--publish-incremental",
        action="store_true",
        help="Upload each clip to GCS as soon as it is cut (implied by --fast)",
    )
    p_recut.add_argument(
        "--clean-work",
        action="store_true",
        help="Wipe local WORK_DIR/<vodId> before restoring from GCS",
    )
    p_recut.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete local WORK_DIR copy after upload",
    )
    p_recut.set_defaults(func=cmd_recut_clips)

    p_clips = sub.add_parser(
        "process-clips",
        help=(
            "Post-process GCS lol_clips/ (no Twitch download): "
            "stitch each game folder into lol_compilations/*_weave.mp4"
        ),
    )
    p_clips.add_argument("--vod-id", required=True)
    p_clips.add_argument(
        "--dataset-dir",
        type=Path,
        default=None,
        help="Use local dataset with lol_clips/ (skip GCS restore)",
    )
    p_clips.add_argument("--work-dir", type=Path, default=None)
    p_clips.add_argument(
        "--min-clips",
        type=int,
        default=2,
        help="Skip games with fewer than N clips after ranking (default: 2)",
    )
    p_clips.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Rank clips and stitch only the best N per game (default: 5). 0 = all.",
    )
    p_clips.add_argument(
        "--no-rank",
        action="store_true",
        help="Skip ranking; stitch every KDA clip (old behavior).",
    )
    p_clips.add_argument(
        "--clips-subdir",
        default=None,
        help=(
            "Clips folder under the VOD prefix (e.g. lol_clips_20260812_001430Z). "
            "Default: completed .cut_run.json clips_subdir, else lol_clips."
        ),
    )
    p_clips.add_argument(
        "--reencode",
        action="store_true",
        help="Ignored. Clip job is stream-copy only.",
    )
    p_clips.add_argument(
        "--detect-freeze",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Drop frozen frames at the start/end of each clip when stitching (default: on)",
    )
    p_clips.add_argument(
        "--clean-work",
        action="store_true",
        help="Wipe local WORK_DIR/<vodId> before restoring clips from GCS",
    )
    p_clips.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete local WORK_DIR copy after upload",
    )
    p_clips.set_defaults(func=cmd_process_clips)

    p_daily = sub.add_parser(
        "process-daily",
        help="Rank every clip from every VOD on a day and stitch one daily compilation",
    )
    p_daily.add_argument(
        "--day-key",
        default=None,
        help="Archive day, e.g. aug12_2026 (America/New_York). Or pass --vod-id.",
    )
    p_daily.add_argument(
        "--vod-id",
        default=None,
        help="Derive --day-key from this VOD's archive folder",
    )
    p_daily.add_argument(
        "--dataset-dir",
        type=Path,
        default=None,
        help="Local folder that already contains lol_clips (possibly nested by vod id)",
    )
    p_daily.add_argument("--work-dir", type=Path, default=None)
    p_daily.add_argument(
        "--top-k",
        type=int,
        default=12,
        help="Keep the best N clips from the whole day (default: 12)",
    )
    p_daily.add_argument(
        "--max-per-game",
        type=int,
        default=3,
        help="Max clips from one game (default: 3). 0 = no cap",
    )
    p_daily.add_argument(
        "--order",
        choices=("chrono", "score"),
        default="chrono",
        help="Clip order in the daily file (default: chrono)",
    )
    p_daily.add_argument(
        "--upload",
        action="store_true",
        help="Upload even when using --dataset-dir",
    )
    p_daily.add_argument("--clean-work", action="store_true")
    p_daily.add_argument("--cleanup", action="store_true")
    p_daily.set_defaults(func=cmd_process_daily)

    p_port = sub.add_parser(
        "process-portraits",
        help=(
            "Post-process GCS lol_compilations/gam*.mp4 (or legacy *_weave.mp4) "
            "into lol_compilations_portrait/*_portrait.mp4 (9:16 TikTok/Shorts)"
        ),
    )
    p_port.add_argument("--vod-id", required=True)
    p_port.add_argument(
        "--dataset-dir",
        type=Path,
        default=None,
        help="Use local dataset with lol_compilations/ (skip GCS restore)",
    )
    p_port.add_argument("--work-dir", type=Path, default=None)
    p_port.add_argument(
        "--max-weaves",
        type=int,
        default=0,
        help="Only render the first N weaves (0 = all). Useful for a single-game test.",
    )
    p_port.add_argument(
        "--intro",
        choices=("overlay", "story", "none"),
        default="overlay",
        help="Opening: overlay on gameplay (default), story=deprecated lobby card, none",
    )
    p_port.add_argument(
        "--outro",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Append the rank-card end outro (default: on)",
    )
    p_port.add_argument(
        "--overlay-hold",
        type=float,
        default=2.0,
        help="Seconds the overlay intro stays up (default: 2)",
    )
    p_port.add_argument(
        "--end-seconds",
        type=float,
        default=2.5,
        help="Rank-card outro length (default: 2.5)",
    )
    p_port.add_argument(
        "--still-seconds",
        type=float,
        default=0.0,
        help="Deprecated. Only used with --intro story (lobby still length)",
    )
    p_port.add_argument(
        "--still-mode",
        choices=("story", "champs", "contain", "cover"),
        default="story",
        help="Deprecated lobby intro crop. Only used with --intro story",
    )
    p_port.add_argument(
        "--game-mode",
        choices=("crop", "fit"),
        default="crop",
        help="Gameplay pane mode (default: crop + KDA PIP)",
    )
    p_port.add_argument(
        "--game-zoom",
        type=float,
        default=0.65,
        help="Crop zoom: 1.0=tight, 0.65=wider + blur bars (default), 0=full-frame fit",
    )
    p_port.add_argument(
        "--cam-hole",
        choices=("black", "keep", "fill"),
        default="fill",
        help="Source webcam hole in the gameplay crop (default: fill)",
    )
    p_port.add_argument(
        "--kda-overlay",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Layer top-right KDA PIP (default: on)",
    )
    p_port.add_argument(
        "--track-champion",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Pan the 9:16 gameplay crop to follow the local player (default: on)",
    )
    p_port.add_argument("--track-fps", type=float, default=4.0)
    p_port.add_argument("--track-dead-zone", type=float, default=0.10)
    p_port.add_argument("--track-ease-ms", type=float, default=280.0)
    p_port.add_argument("--track-max-speed", type=float, default=860.0)
    p_port.add_argument("--track-self-bias", type=float, default=0.50)
    p_port.add_argument("--track-enemy-pull", type=float, default=0.0)
    p_port.add_argument("--track-pan-cooldown", type=float, default=3.0)
    p_port.add_argument("--track-outside-hold", type=float, default=1.5)
    p_port.add_argument(
        "--music",
        default="auto",
        help="Lofi bed: auto=random catalog pick (default), off, or catalog id",
    )
    p_port.add_argument("--music-db", type=float, default=-20.0)
    p_port.add_argument("--preset", default="veryfast", help="x264 preset (default: veryfast)")
    p_port.add_argument("--crf", type=int, default=20)
    p_port.add_argument(
        "--force",
        action="store_true",
        help="Re-render even if portrait mp4 already exists locally",
    )
    p_port.add_argument(
        "--clean-work",
        action="store_true",
        help="Wipe local WORK_DIR/<vodId> before restoring compilations from GCS",
    )
    p_port.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete local WORK_DIR copy after upload",
    )
    p_port.set_defaults(func=cmd_process_portraits)

    p_seg = sub.add_parser(
        "process-segmented",
        help="Split a long VOD into time segments; download/cut/upload each (resume-friendly)",
    )
    p_seg.add_argument("--vod-id", required=True)
    p_seg.add_argument("--url", default=None)
    p_seg.add_argument("--work-dir", type=Path, default=None)
    p_seg.add_argument(
        "--segment-seconds",
        type=float,
        default=10800.0,
        help="Core segment length in seconds (default: 3h)",
    )
    p_seg.add_argument(
        "--segment-pad",
        type=float,
        default=30.0,
        help="Overlap pad downloaded on each side (default: 30s)",
    )
    p_seg.add_argument(
        "--max-segments",
        type=int,
        default=0,
        help="Only process the first N segments (0 = all). Useful for smoke tests.",
    )
    p_seg.add_argument("--types", default="KILL,DEATH,ASSIST")
    p_seg.add_argument("--max-clips", type=int, default=0)
    p_seg.add_argument(
        "--force",
        action="store_true",
        help="Re-process segments even if segment_manifest.json exists",
    )
    p_seg.add_argument(
        "--keep-empty-segments",
        action="store_true",
        help="Download segments even when they contain no LoL events",
    )
    p_seg.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete local segment scratch dirs after upload",
    )
    p_seg.set_defaults(func=cmd_process_segmented)

    p_night = sub.add_parser(
        "nightly",
        help="Archive new Twitch VODs to GCS + index (uses WORK_DIR, not repo data/)",
    )
    p_night.add_argument("--limit", type=int, default=5)
    p_night.add_argument("--work-dir", type=Path, default=None)
    p_night.add_argument(
        "--force",
        action="store_true",
        help="Re-process even if archive_manifest.json exists",
    )
    p_night.add_argument(
        "--force-redownload",
        action="store_true",
        help="Ignore GCS source checkpoints and re-download from Twitch",
    )
    p_night.add_argument("--dry-run", action="store_true")
    p_night.add_argument("--cut-clips", action="store_true", help="Also cut KDA clips before upload")
    p_night.add_argument("--types", default="KILL,DEATH,ASSIST")
    p_night.add_argument("--max-clips", type=int, default=0)
    p_night.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete local WORK_DIR copy after upload (GCS kept)",
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
