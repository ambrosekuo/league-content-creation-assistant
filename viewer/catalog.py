"""Merge local data/ with GCS vods/{day}/{id}/ into a browseable catalog."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dataset_paths import (
    day_key_from_dir_name,
    find_dataset_dir,
    iter_local_vod_dirs,
    vod_id_from_dir_name,
)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

_MONTHS = (
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
)


def _day_sort_key(day: str) -> tuple:
    match = re.fullmatch(r"([a-z]{3})(\d{2})_(\d{4})", day or "")
    if not match or match.group(1) not in _MONTHS:
        return (0, 0, 0, 0, day or "")
    return (1, int(match.group(3)), _MONTHS.index(match.group(1)), int(match.group(2)), day)


SKIP_DIR_PREFIXES = ("_", ".")
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

_GAME_STEM = re.compile(
    r"^(?P<stem>gam\d+_.+?)(?:_portrait|_wide|_tracked|_topk|_lobby_story|_hold[\d.]+s)*$",
    re.I,
)


def _gcs():
    from env_loader import load_dotenv

    load_dotenv()
    import storage_gcs as gcs

    return gcs


_INDEX_CACHE: dict[str, Any] = {"at": 0.0, "index": None, "error": None}


def gcs_ready() -> bool:
    try:
        gcs = _gcs()
        return gcs.is_configured()
    except Exception:
        return False


def gcs_index(*, ttl: float = 60.0) -> list[dict[str, Any]]:
    now = time.time()
    cached = _INDEX_CACHE.get("index")
    if cached is not None and now - float(_INDEX_CACHE["at"] or 0) < ttl:
        return cached
    gcs = _gcs()
    index = gcs.list_archive_index()
    _INDEX_CACHE["at"] = now
    _INDEX_CACHE["index"] = index
    _INDEX_CACHE["error"] = None
    return index


def resolve_gcs_day(vod_id: str, day_key: str | None = None) -> str | None:
    """Find vods/{day}/{id}/ even when the local folder has no metadata."""
    vid = vod_id.strip().lstrip("v")
    day = (day_key or "").strip().lower()
    if day and day != "local":
        return day
    if not gcs_ready():
        return None
    try:
        for row in gcs_index():
            if vid in (row.get("vodIds") or []):
                return row["dayKey"]
    except Exception:
        return None
    return None


def gcs_object_name(vod_id: str, rel: str, *, day_key: str | None = None) -> str | None:
    gcs = _gcs()
    day = resolve_gcs_day(vod_id, day_key)
    if not day:
        return None
    vid = vod_id.strip().lstrip("v")
    return f"{gcs.prefix()}/{day}/{vid}/{rel.lstrip('/')}"


def _kind_for(rel: str) -> str:
    parts = rel.replace("\\", "/").split("/")
    joined = "/".join(parts)
    if "_daily" in parts or "lol_compilations_daily" in parts:
        return "daily"
    if "lol_compilations_picks_portrait" in parts or "lol_compilations_portrait" in parts:
        return "portrait"
    if "lol_compilations_picks" in parts or "lol_compilations_topk" in parts:
        return "weave"
    if "lol_compilations" in parts:
        return "weave"
    if any(p.startswith("lol_clips") for p in parts):
        return "clip"
    if parts[-1].startswith("source."):
        return "source"
    if joined.endswith(".mp4"):
        return "other"
    return "sidecar"


def _meta_from_file(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    ts = raw.get("timestamp")
    duration = raw.get("duration")
    return {
        "title": raw.get("title") or raw.get("fulltitle"),
        "timestamp": ts,
        "duration": duration,
        "uploader": raw.get("uploader") or raw.get("uploader_id"),
        "url": raw.get("webpage_url") or raw.get("original_url"),
    }


def _day_from_meta(meta: dict[str, Any]) -> str | None:
    ts = meta.get("timestamp")
    if ts is None:
        return None
    try:
        gcs = _gcs()
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        return gcs.day_key_from_dt(dt)
    except Exception:
        return None


def _rel_of(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def scan_local_vod(dataset_dir: Path) -> dict[str, Any]:
    vid = vod_id_from_dir_name(dataset_dir.name)
    meta: dict[str, Any] = {}
    for name in ("metadata.json", "source.info.json"):
        path = dataset_dir / name
        if path.is_file():
            meta = _meta_from_file(path)
            if meta:
                break
    videos: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    folders: set[str] = set()
    for path in dataset_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = _rel_of(path, dataset_dir)
        top = rel.split("/", 1)[0]
        if top.startswith(SKIP_DIR_PREFIXES):
            continue
        if "pan_compare" in rel.split("/") or "/intro_test/" in f"/{rel}/" or "/lane_test/" in f"/{rel}/":
            continue
        if ".partial" in path.name or ".part-" in path.name or path.name.endswith(".part"):
            continue
        suffix = path.suffix.lower()
        stat = path.stat()
        item = {
            "path": rel,
            "name": path.name,
            "size": stat.st_size,
            "updated": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "kind": _kind_for(rel),
            "local": True,
            "gcs": False,
        }
        if suffix in VIDEO_EXTS:
            videos.append(item)
            if "/" in rel:
                folders.add(rel.rsplit("/", 1)[0])
        elif suffix in IMAGE_EXTS:
            images.append(item)

    flags = {
        "source": any(v["kind"] == "source" for v in videos),
        "clips": any(v["kind"] == "clip" for v in videos),
        "weaves": any(v["kind"] == "weave" for v in videos),
        "portraits": any(v["kind"] == "portrait" for v in videos),
        "daily": any(v["kind"] == "daily" for v in videos),
        "events": (dataset_dir / "lol_events.json").is_file(),
        "manifest": (dataset_dir / "archive_manifest.json").is_file(),
    }
    return {
        "vodId": vid,
        "localName": dataset_dir.name,
        "dayKey": day_key_from_dir_name(dataset_dir.name) or _day_from_meta(meta) or "local",
        "title": meta.get("title"),
        "timestamp": meta.get("timestamp"),
        "duration": meta.get("duration"),
        "uploader": meta.get("uploader"),
        "url": meta.get("url"),
        "localDir": str(dataset_dir),
        "local": True,
        "gcs": False,
        "flags": flags,
        "videos": videos,
        "images": images,
        "folders": sorted(folders),
    }


def _gcs_vod_detail(day_key: str, vod_id: str) -> dict[str, Any]:
    gcs = _gcs()
    base = f"{gcs.prefix()}/{day_key}/{vod_id}"
    objects = gcs.list_objects(f"{base}/")
    meta: dict[str, Any] = {}
    videos: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    folders: set[str] = set()
    flags = {
        "source": False,
        "clips": False,
        "weaves": False,
        "portraits": False,
        "daily": False,
        "events": False,
        "manifest": False,
    }
    for obj in objects:
        rel = obj["name"]
        kind = _kind_for(rel)
        name = rel.rsplit("/", 1)[-1]
        if name == "metadata.json":
            try:
                raw = gcs.read_json_blob(obj["object"])
                meta = {
                    "title": raw.get("title") or raw.get("fulltitle"),
                    "timestamp": raw.get("timestamp"),
                    "duration": raw.get("duration"),
                    "uploader": raw.get("uploader") or raw.get("uploader_id"),
                    "url": raw.get("webpage_url") or raw.get("original_url"),
                }
            except Exception:
                pass
        if name == "lol_events.json":
            flags["events"] = True
        if name == "archive_manifest.json":
            flags["manifest"] = True
        lower = name.lower()
        item = {
            "path": rel,
            "name": name,
            "size": obj["size"],
            "updated": obj.get("updated"),
            "kind": kind,
            "local": False,
            "gcs": True,
            "object": obj["object"],
        }
        if any(lower.endswith(ext) for ext in VIDEO_EXTS):
            videos.append(item)
            if "/" in rel:
                folders.add(rel.rsplit("/", 1)[0])
            if kind == "source":
                flags["source"] = True
            elif kind == "clip":
                flags["clips"] = True
            elif kind == "weave":
                flags["weaves"] = True
            elif kind == "portrait":
                flags["portraits"] = True
            elif kind == "daily":
                flags["daily"] = True
        elif any(lower.endswith(ext) for ext in IMAGE_EXTS):
            images.append(item)
    return {
        "vodId": vod_id,
        "dayKey": day_key,
        "title": meta.get("title"),
        "timestamp": meta.get("timestamp"),
        "duration": meta.get("duration"),
        "uploader": meta.get("uploader"),
        "url": meta.get("url"),
        "gcsPrefix": base,
        "local": False,
        "gcs": True,
        "flags": flags,
        "videos": videos,
        "images": images,
        "folders": sorted(folders),
    }


def _merge_vod(local: dict[str, Any] | None, remote: dict[str, Any] | None) -> dict[str, Any]:
    if local and not remote:
        return local
    if remote and not local:
        return remote
    assert local and remote
    by_path: dict[str, dict[str, Any]] = {}
    for item in remote["videos"] + local["videos"]:
        existing = by_path.get(item["path"])
        if existing is None:
            by_path[item["path"]] = dict(item)
            continue
        existing["local"] = existing.get("local") or item.get("local")
        existing["gcs"] = existing.get("gcs") or item.get("gcs")
        if item.get("local"):
            existing["size"] = item["size"]
            existing["updated"] = item.get("updated") or existing.get("updated")
        if item.get("object"):
            existing["object"] = item["object"]
    images_by: dict[str, dict[str, Any]] = {}
    for item in remote["images"] + local["images"]:
        images_by[item["path"]] = item
    flags = dict(remote["flags"])
    for key, val in local["flags"].items():
        flags[key] = bool(flags.get(key) or val)
    return {
        "vodId": local["vodId"],
        "dayKey": remote.get("dayKey") or local.get("dayKey") or "local",
        "title": local.get("title") or remote.get("title"),
        "timestamp": local.get("timestamp") or remote.get("timestamp"),
        "duration": local.get("duration") or remote.get("duration"),
        "uploader": local.get("uploader") or remote.get("uploader"),
        "url": local.get("url") or remote.get("url"),
        "localName": local.get("localName"),
        "localDir": local.get("localDir"),
        "gcsPrefix": remote.get("gcsPrefix"),
        "local": True,
        "gcs": True,
        "flags": flags,
        "videos": sorted(by_path.values(), key=lambda v: v["path"]),
        "images": sorted(images_by.values(), key=lambda v: v["path"]),
        "folders": sorted(set(local.get("folders") or []) | set(remote.get("folders") or [])),
    }


def _summarize(vod: dict[str, Any]) -> dict[str, Any]:
    videos = vod.get("videos") or []
    return {
        "vodId": vod["vodId"],
        "localName": vod.get("localName"),
        "dayKey": vod.get("dayKey") or "local",
        "title": vod.get("title"),
        "timestamp": vod.get("timestamp"),
        "duration": vod.get("duration"),
        "local": bool(vod.get("local")),
        "gcs": bool(vod.get("gcs")),
        "flags": vod.get("flags") or {},
        "videoCount": len(videos),
        "portraitCount": sum(1 for v in videos if v.get("kind") == "portrait"),
        "weaveCount": sum(1 for v in videos if v.get("kind") == "weave"),
        "clipCount": sum(1 for v in videos if v.get("kind") == "clip"),
    }


def build_catalog(*, include_gcs: bool = True) -> dict[str, Any]:
    local_map: dict[str, dict[str, Any]] = {}
    for folder in iter_local_vod_dirs(DATA):
        rec = scan_local_vod(folder)
        local_map[rec["vodId"]] = rec

    remote_index: list[dict[str, Any]] = []
    bucket = None
    gcs_error = None
    if include_gcs and gcs_ready():
        try:
            gcs = _gcs()
            bucket = gcs.bucket_name()
            remote_index = gcs_index()
        except Exception as exc:
            gcs_error = str(exc)
            remote_index = []

    days: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()

    for day_row in remote_index:
        day = day_row["dayKey"]
        entry = days.setdefault(day, {"dayKey": day, "hasDaily": False, "vods": []})
        entry["hasDaily"] = bool(day_row.get("hasDaily"))
        for vid in day_row.get("vodIds") or []:
            local = local_map.get(vid)
            summary_src = local or {
                "vodId": vid,
                "dayKey": day,
                "local": False,
                "gcs": True,
                "flags": {},
                "videos": [],
            }
            if local:
                summary_src = {
                    **local,
                    "dayKey": day,
                    "gcs": True,
                    "gcsPrefix": f"vods/{day}/{vid}",
                }
            summary = _summarize(summary_src)
            summary["gcs"] = True
            summary["dayKey"] = day
            entry["vods"].append(summary)
            seen.add(vid)

    for vid, rec in local_map.items():
        if vid in seen:
            continue
        day = rec.get("dayKey") or "local"
        entry = days.setdefault(day, {"dayKey": day, "hasDaily": False, "vods": []})
        entry["vods"].append(_summarize(rec))

    dated = [k for k in days if k != "local"]
    ordered_keys = (["local"] if "local" in days else []) + sorted(
        dated, key=_day_sort_key, reverse=True
    )
    ordered = []
    for key in ordered_keys:
        row = days[key]
        row["vods"] = sorted(row["vods"], key=lambda v: v["vodId"], reverse=True)
        ordered.append(row)

    return {
        "bucket": bucket,
        "gcs": bool(bucket) and gcs_error is None,
        "gcsError": gcs_error,
        "localData": str(DATA),
        "days": ordered,
    }


def get_vod(vod_id: str, *, day_key: str | None = None) -> dict[str, Any]:
    vid = vod_id.strip().lstrip("v")
    local_dir = find_dataset_dir(DATA, vid)
    local = scan_local_vod(local_dir) if local_dir and local_dir.is_dir() else None
    remote = None
    day = resolve_gcs_day(vid, day_key)
    if day:
        try:
            remote = _gcs_vod_detail(day, vid)
        except Exception:
            remote = None
    if not local and not remote:
        raise FileNotFoundError(vid)
    return _merge_vod(local, remote)


def resolve_local(vod_id: str, rel: str) -> Path:
    vid = vod_id.strip().lstrip("v")
    dataset_dir = find_dataset_dir(DATA, vid)
    if dataset_dir is None:
        raise FileNotFoundError(vid)
    base = dataset_dir.resolve()
    path = (base / rel).resolve()
    if base not in path.parents and path != base:
        raise ValueError("path escapes dataset dir")
    if not path.is_file():
        raise FileNotFoundError(rel)
    return path


def poster_for(vod: dict[str, Any], video_path: str) -> str | None:
    images = {img["path"]: img for img in vod.get("images") or []}
    stem = Path(video_path).stem
    folder = str(Path(video_path).parent).replace("\\", "/")
    for candidate in (
        f"{folder}/{stem}.jpg",
        f"{folder}/{stem}.png",
        f"source.jpg",
    ):
        if candidate in images:
            return candidate
    match = _GAME_STEM.match(stem)
    game = match.group("stem") if match else stem
    for img_path in images:
        name = Path(img_path).name
        if name.startswith(game) and name.endswith("_lobby.png"):
            return img_path
        if "lol_compilations/" in img_path.replace("\\", "/") and name.endswith("_lobby.png"):
            if game.split("_")[0] in name:
                return img_path
    return None
