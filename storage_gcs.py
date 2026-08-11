"""GCS helpers for archiving VOD datasets. Optional if google-cloud-storage missing."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


class GCSNotConfiguredError(RuntimeError):
    pass


_MONTHS = (
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)


def bucket_name() -> str:
    name = (os.environ.get("GCS_BUCKET") or "").strip()
    if not name:
        raise GCSNotConfiguredError("GCS_BUCKET is not set")
    return name


def prefix() -> str:
    return (os.environ.get("GCS_PREFIX") or "vods").strip().strip("/")


def archive_tz() -> ZoneInfo:
    name = (os.environ.get("GCS_DAY_TZ") or "America/New_York").strip()
    return ZoneInfo(name)


def day_key_from_dt(dt: datetime) -> str:
    """Format like aug10_2026 in the archive timezone."""
    local = dt.astimezone(archive_tz())
    return f"{_MONTHS[local.month - 1]}{local.day:02d}_{local.year}"


def day_key_from_dataset(dataset_dir: Path | None = None) -> str | None:
    """Derive day key from env override or local metadata/source.info.json."""
    override = (os.environ.get("GCS_DAY_KEY") or "").strip()
    if override:
        return override.lower()

    if dataset_dir is None:
        return None
    for name in ("metadata.json", "source.info.json"):
        path = dataset_dir / name
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        ts = payload.get("timestamp")
        if ts is None:
            continue
        try:
            dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            continue
        return day_key_from_dt(dt)
    return None


def find_existing_day_key(vod_id: str) -> str | None:
    """Scan GCS for vods/{day}/{vod_id}/ or work/{day}/{vod_id}/."""
    vid = vod_id.strip().lstrip("v")
    client = _client()
    bucket = client.bucket(bucket_name())
    work_root = (os.environ.get("GCS_WORK_PREFIX") or "work").strip().strip("/")
    for root in (prefix(), work_root):
        for blob in client.list_blobs(bucket, prefix=f"{root}/", max_results=5000):
            parts = blob.name.split("/")
            # vods/aug10_2026/{id}/...
            if len(parts) >= 3 and parts[1] and parts[2] == vid:
                if re.fullmatch(r"[a-z]{3}\d{2}_\d{4}", parts[1]):
                    return parts[1]
    return None


def resolve_day_key(vod_id: str, dataset_dir: Path | None = None) -> str:
    """
    Day folder for this VOD.

    Order: GCS_DAY_KEY env → local metadata timestamp → existing GCS layout → UTC today.
    """
    keyed = day_key_from_dataset(dataset_dir)
    if keyed:
        return keyed

    found = find_existing_day_key(vod_id)
    if found:
        return found

    return day_key_from_dt(datetime.now(timezone.utc))


def vod_prefix(vod_id: str, *, day_key: str | None = None, dataset_dir: Path | None = None) -> str:
    """Canonical archive prefix: vods/{aug10_2026}/{vodId}/."""
    vid = vod_id.strip().lstrip("v")
    day = day_key or resolve_day_key(vid, dataset_dir)
    return f"{prefix()}/{day}/{vid}"


def legacy_vod_prefix(vod_id: str) -> str:
    """Old flat layout vods/{vodId}/ (pre day folders)."""
    return f"{prefix()}/{vod_id.strip().lstrip('v')}"


def _client():
    try:
        from google.cloud import storage  # type: ignore
    except ImportError as exc:
        raise GCSNotConfiguredError(
            "google-cloud-storage is not installed. "
            "pip install -r requirements-cloud.txt"
        ) from exc
    return storage.Client()


def blob_exists(object_name: str) -> bool:
    client = _client()
    bucket = client.bucket(bucket_name())
    return bucket.blob(object_name).exists()


def upload_file(local_path: Path, object_name: str, *, content_type: str | None = None) -> str:
    client = _client()
    bucket = client.bucket(bucket_name())
    blob = bucket.blob(object_name)
    blob.upload_from_filename(str(local_path), content_type=content_type)
    return f"gs://{bucket_name()}/{object_name}"


def delete_prefix(object_prefix: str) -> int:
    """Delete all objects under a GCS prefix. Returns number deleted."""
    client = _client()
    bucket = client.bucket(bucket_name())
    blobs = list(client.list_blobs(bucket, prefix=object_prefix))
    deleted = 0
    for blob in blobs:
        blob.delete()
        deleted += 1
    return deleted


def upload_json(payload: dict[str, Any] | list[Any], object_name: str) -> str:
    client = _client()
    bucket = client.bucket(bucket_name())
    blob = bucket.blob(object_name)
    blob.upload_from_string(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        content_type="application/json",
    )
    return f"gs://{bucket_name()}/{object_name}"


def download_file(object_name: str, local_path: Path) -> Path:
    client = _client()
    bucket = client.bucket(bucket_name())
    blob = bucket.blob(object_name)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    blob.download_to_filename(str(local_path))
    return local_path


def list_vod_ids() -> list[str]:
    """List vod ids under vods/{id}/ or vods/{day}/{id}/."""
    client = _client()
    bucket = client.bucket(bucket_name())
    root = f"{prefix()}/"
    ids: set[str] = set()
    for blob in client.list_blobs(bucket, prefix=root):
        parts = blob.name[len(root) :].split("/")
        if len(parts) >= 2 and parts[-1] == "metadata.json":
            # vods/{id}/metadata.json OR vods/{day}/{id}/metadata.json
            if len(parts) == 2:
                ids.add(parts[0])
            elif len(parts) == 3:
                ids.add(parts[1])
    return sorted(ids)


def vod_archived(vod_id: str, *, dataset_dir: Path | None = None) -> bool:
    """True if a completed archive_manifest exists (full pipeline finished)."""
    vid = vod_id.strip().lstrip("v")
    candidates = [
        f"{vod_prefix(vid, dataset_dir=dataset_dir)}/archive_manifest.json",
        f"{legacy_vod_prefix(vid)}/archive_manifest.json",
    ]
    day = find_existing_day_key(vid)
    if day:
        candidates.insert(0, f"{prefix()}/{day}/{vid}/archive_manifest.json")
    return any(blob_exists(name) for name in candidates)


def work_prefix(vod_id: str, *, day_key: str | None = None, dataset_dir: Path | None = None) -> str:
    """Incomplete / in-progress working prefix (durable across job retries)."""
    root = (os.environ.get("GCS_WORK_PREFIX") or "work").strip().strip("/")
    vid = vod_id.strip().lstrip("v")
    day = day_key or resolve_day_key(vid, dataset_dir)
    return f"{root}/{day}/{vid}"


def legacy_work_prefix(vod_id: str) -> str:
    root = (os.environ.get("GCS_WORK_PREFIX") or "work").strip().strip("/")
    return f"{root}/{vod_id.strip().lstrip('v')}"


def blob_size(object_name: str) -> int | None:
    client = _client()
    bucket = client.bucket(bucket_name())
    blob = bucket.blob(object_name)
    if not blob.exists():
        return None
    blob.reload()
    return int(blob.size or 0)


def find_source_checkpoint(vod_id: str, *, dataset_dir: Path | None = None) -> str | None:
    """
    Return object name of a reusable source.mp4 checkpoint, if any.

    Prefers dated vods/{day}/{id}/, then work/, then legacy flat paths.
    """
    min_bytes = int(os.environ.get("GCS_SOURCE_MIN_BYTES") or str(50 * 1024 * 1024))
    vid = vod_id.strip().lstrip("v")
    day = None
    try:
        day = resolve_day_key(vid, dataset_dir)
    except Exception:
        day = find_existing_day_key(vid)

    work_root = (os.environ.get("GCS_WORK_PREFIX") or "work").strip().strip("/")
    candidates: list[str] = []
    if day:
        candidates.extend(
            [
                f"{prefix()}/{day}/{vid}/source.mp4",
                f"{work_root}/{day}/{vid}/source.mp4",
            ]
        )
    existing = find_existing_day_key(vid)
    if existing and existing != day:
        candidates.extend(
            [
                f"{prefix()}/{existing}/{vid}/source.mp4",
                f"{work_root}/{existing}/{vid}/source.mp4",
            ]
        )
    candidates.extend(
        [
            f"{legacy_vod_prefix(vid)}/source.mp4",
            f"{legacy_work_prefix(vid)}/source.mp4",
        ]
    )
    for name in candidates:
        size = blob_size(name)
        if size is not None and size >= min_bytes:
            return name
    return None


def checkpoint_source_files(dataset_dir: Path, vod_id: str) -> dict[str, str]:
    """
    Upload durable source artifacts right after Twitch download.

    Writes to both work/ (resume) and vods/ (canonical) so later steps can
    skip re-downloading from Twitch if index/cut fails.
    """
    dataset_dir = dataset_dir.resolve()
    vid = vod_id.strip().lstrip("v")
    day = resolve_day_key(vid, dataset_dir)
    uploaded: dict[str, str] = {}

    source = None
    for candidate in sorted(dataset_dir.glob("source.*")):
        if candidate.suffix.lower() in {".mp4", ".mkv", ".mov", ".webm"} and candidate.is_file():
            if ".part" in candidate.name:
                continue
            source = candidate
            break
    if source is None:
        raise FileNotFoundError(f"No source media in {dataset_dir}")

    for base in (work_prefix(vid, day_key=day), vod_prefix(vid, day_key=day)):
        uploaded[f"{base}/{source.name}"] = upload_file(
            source, f"{base}/{source.name}", content_type="video/mp4"
        )
        for name in ("metadata.json", "ingest.json", "source.info.json", "source.jpg"):
            path = dataset_dir / name
            if path.is_file():
                ctype = "application/json" if path.suffix == ".json" else None
                uploaded[f"{base}/{name}"] = upload_file(path, f"{base}/{name}", content_type=ctype)

    uploaded["_checkpoint"] = upload_json(
        {
            "vodId": vid,
            "dayKey": day,
            "sourceObject": f"{vod_prefix(vid, day_key=day)}/{source.name}",
            "sourceBytes": source.stat().st_size,
            "stage": "source_checkpoint",
        },
        f"{work_prefix(vid, day_key=day)}/checkpoint.json",
    )
    print(f"[checkpoint] day={day} → gs://{bucket_name()}/{vod_prefix(vid, day_key=day)}/", flush=True)
    return uploaded


def restore_source_checkpoint(vod_id: str, dataset_dir: Path) -> Path:
    """Download a GCS source checkpoint into dataset_dir for resume."""
    object_name = find_source_checkpoint(vod_id, dataset_dir=dataset_dir)
    if not object_name:
        raise FileNotFoundError(f"No GCS source checkpoint for {vod_id}")

    dataset_dir.mkdir(parents=True, exist_ok=True)
    local_source = dataset_dir / "source.mp4"
    print(f"[resume] downloading gs://{bucket_name()}/{object_name} → {local_source}", flush=True)
    download_file(object_name, local_source)

    side_prefix = object_name.rsplit("/", 1)[0]
    vid = vod_id.strip().lstrip("v")
    extra_prefixes = [
        side_prefix,
        vod_prefix(vid, dataset_dir=dataset_dir),
        work_prefix(vid, dataset_dir=dataset_dir),
        legacy_vod_prefix(vid),
        legacy_work_prefix(vid),
    ]
    seen: set[str] = set()
    for side in extra_prefixes:
        if side in seen:
            continue
        seen.add(side)
        for name in (
            "metadata.json",
            "ingest.json",
            "source.info.json",
            "source.jpg",
            "lol_events.json",
            "transcript.json",
        ):
            dest = dataset_dir / name
            if dest.is_file():
                continue
            remote = f"{side}/{name}"
            if blob_exists(remote):
                print(f"[resume] sidecar {remote}", flush=True)
                download_file(remote, dest)

    meta = dataset_dir / "metadata.json"
    info = dataset_dir / "source.info.json"
    if not meta.is_file() and info.is_file():
        meta.write_bytes(info.read_bytes())
        print("[resume] copied source.info.json → metadata.json", flush=True)

    if not meta.is_file():
        raise FileNotFoundError(
            f"Resume missing metadata.json for {vod_id} "
            f"(checked vods/ and work/ in gs://{bucket_name()})"
        )

    return local_source


def upload_clip_artifacts(dataset_dir: Path, *, vod_id: str | None = None) -> dict[str, str]:
    """
    Re-upload snap/cut outputs without touching source.mp4.

    Replaces lol_clips/ in GCS so old long clips do not linger under new names.
    """
    dataset_dir = dataset_dir.resolve()
    vid = (vod_id or dataset_dir.name).strip().lstrip("v")
    day = resolve_day_key(vid, dataset_dir)
    base = vod_prefix(vid, day_key=day)
    uploaded: dict[str, str] = {}

    clips_prefix = f"{base}/lol_clips/"
    removed = delete_prefix(clips_prefix)
    if removed:
        print(f"[upload] cleared {removed} old object(s) under {clips_prefix}", flush=True)

    targets = [
        dataset_dir / "lol_events_snapped.json",
        dataset_dir / "archive_manifest.json",
        dataset_dir / "_upload_manifest.json",
    ]
    clips_dir = dataset_dir / "lol_clips"
    if clips_dir.is_dir():
        targets.extend(sorted(p for p in clips_dir.rglob("*") if p.is_file()))

    for path in targets:
        if not path.is_file():
            continue
        rel = path.relative_to(dataset_dir).as_posix()
        object_name = f"{base}/{rel}"
        content_type = "application/json" if path.suffix.lower() == ".json" else None
        if path.suffix.lower() == ".mp4":
            content_type = "video/mp4"
        print(f"[upload] {rel}", flush=True)
        uploaded[rel] = upload_file(path, object_name, content_type=content_type)

    return uploaded


def upload_dataset_dir(dataset_dir: Path, *, vod_id: str | None = None) -> dict[str, str]:
    """
    Upload a completed local dataset folder to GCS.

    Skips partial downloads (*.part, *.ytdl). Safe to call on finished datasets only.
    """
    dataset_dir = dataset_dir.resolve()
    if not dataset_dir.is_dir():
        raise FileNotFoundError(dataset_dir)

    vid = (vod_id or dataset_dir.name).strip().lstrip("v")
    day = resolve_day_key(vid, dataset_dir)
    base = vod_prefix(vid, day_key=day)
    uploaded: dict[str, str] = {}

    skip_suffixes = {".part", ".ytdl"}
    for path in sorted(dataset_dir.rglob("*")):
        if not path.is_file():
            continue
        name = path.name
        if name.endswith(".part") or name.endswith(".ytdl") or ".part-" in name:
            continue
        if path.suffix.lower() in skip_suffixes:
            continue

        rel = path.relative_to(dataset_dir).as_posix()
        object_name = f"{base}/{rel}"
        content_type = None
        if path.suffix.lower() == ".json":
            content_type = "application/json"
        elif path.suffix.lower() == ".mp4":
            content_type = "video/mp4"
        uploaded[rel] = upload_file(path, object_name, content_type=content_type)

    manifest = {
        "vodId": vid,
        "dayKey": day,
        "datasetDir": str(dataset_dir),
        "objects": uploaded,
    }
    uploaded["_manifest.json"] = upload_json(manifest, f"{base}/_upload_manifest.json")
    print(f"[upload] gs://{bucket_name()}/{base}/", flush=True)
    return uploaded
