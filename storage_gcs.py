"""GCS helpers for archiving VOD datasets. Optional if google-cloud-storage missing."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class GCSNotConfiguredError(RuntimeError):
    pass


def bucket_name() -> str:
    name = (os.environ.get("GCS_BUCKET") or "").strip()
    if not name:
        raise GCSNotConfiguredError("GCS_BUCKET is not set")
    return name


def prefix() -> str:
    return (os.environ.get("GCS_PREFIX") or "vods").strip().strip("/")


def vod_prefix(vod_id: str) -> str:
    return f"{prefix()}/{vod_id.strip()}"


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
    """List vod ids that have a metadata.json under the configured prefix."""
    client = _client()
    bucket = client.bucket(bucket_name())
    root = f"{prefix()}/"
    ids: set[str] = set()
    for blob in client.list_blobs(bucket, prefix=root):
        # vods/{id}/metadata.json
        parts = blob.name[len(root) :].split("/")
        if len(parts) >= 2 and parts[1] == "metadata.json":
            ids.add(parts[0])
    return sorted(ids)


def vod_archived(vod_id: str) -> bool:
    """True if source media or metadata already exists in GCS."""
    base = vod_prefix(vod_id)
    return blob_exists(f"{base}/metadata.json") or blob_exists(f"{base}/source.mp4")


def upload_dataset_dir(dataset_dir: Path, *, vod_id: str | None = None) -> dict[str, str]:
    """
    Upload a completed local dataset folder to GCS.

    Skips partial downloads (*.part, *.ytdl). Safe to call on finished datasets only.
    """
    dataset_dir = dataset_dir.resolve()
    if not dataset_dir.is_dir():
        raise FileNotFoundError(dataset_dir)

    vid = (vod_id or dataset_dir.name).strip().lstrip("v")
    base = vod_prefix(vid)
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
        "datasetDir": str(dataset_dir),
        "objects": uploaded,
    }
    uploaded["_manifest.json"] = upload_json(manifest, f"{base}/_upload_manifest.json")
    return uploaded
