"""Build a clip-review list from lol_clips/clips.json (local or GCS)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from dataset_paths import day_key_from_dir_name, find_dataset_dir, resolve_dataset_dir
from viewer import catalog
from viewer.store import (
    CLIP_RATINGS,
    clip_is_classified,
    get_classifications,
    get_clip_selections,
    get_titles,
    normalize_classification_entry,
    title_is_generated,
)

DATA = catalog.DATA
FOLDER_RE = re.compile(
    r"^g(?P<game>\d+)_(?P<champ>[A-Za-z0-9]+)(?:_vs(?P<lane>[A-Za-z0-9]+))?$",
    re.I,
)
STEM_RE = re.compile(
    r"^c(?P<idx>\d+)_(?P<label>.+?)(?:_vs(?P<opp>[A-Za-z0-9]+))?$",
    re.I,
)


def clip_id(game_index: int, clip_index: int) -> str:
    return f"g{int(game_index):02d}_c{int(clip_index):02d}"


def resolve_day(vod_id: str, day_key: str | None = None) -> str:
    day = (day_key or "").strip().lower()
    if day and day != "local":
        return day
    vid = vod_id.strip().lstrip("v")
    dataset = find_dataset_dir(DATA, vid)
    if dataset is not None:
        from_name = day_key_from_dir_name(dataset.name)
        if from_name:
            return from_name
    found = catalog.resolve_gcs_day(vid, day_key)
    return found or "local"


def summarize(
    clips: list[dict[str, Any]],
    selections: dict[str, Any],
    classifications: dict[str, Any] | None = None,
    *,
    exports: dict[str, Any] | None = None,
    titles: dict[str, Any] | None = None,
) -> dict[str, int]:
    counts = {rating: 0 for rating in CLIP_RATINGS}
    unreviewed = 0
    classified = 0
    class_map = classifications or {}
    for clip in clips:
        rec = selections.get(clip["id"]) or {}
        rating = rec.get("rating")
        if rating in counts:
            counts[rating] += 1
        else:
            unreviewed += 1
        if clip_is_classified(class_map.get(clip["id"])):
            classified += 1
    title_map = titles or {}
    decorated = (exports or {}).get("decorated") or []
    titled = sum(
        1 for row in decorated if title_is_generated(title_map.get(str(row.get("weaveStem") or "")))
    )
    music = (exports or {}).get("music") or []
    posted = sum(1 for row in music if post_is_done(row.get("post")))
    return {
        "total": len(clips),
        "reviewed": len(clips) - unreviewed,
        "unreviewed": unreviewed,
        "classified": classified,
        "unclassified": len(clips) - classified,
        "titled": titled,
        "untitled": len(decorated) - titled,
        "posted": posted,
        "unposted": len(music) - posted,
        "keep": counts["keep"],
        "excellent": counts["excellent"],
        "godly": counts["godly"],
        "manual_edit": counts["manual_edit"],
        "rejected": counts["reject"],
        "local": sum(1 for clip in clips if clip.get("local")),
        "gcsOnly": sum(1 for clip in clips if not clip.get("local")),
    }


WEAVE_STEM_RE = re.compile(
    r"^gam(?P<game>\d+)_(?P<champ>[a-z0-9]+)(?:_vs_(?P<opp>[a-z0-9]+))?(?:_(?P<result>win|loss))?$",
    re.I,
)
SKIP_EXPORT_BITS = (
    "_captioned",
    "_pool",
    "_music",
    "_nomusic",
    "lobby",
    "preview",
    "_combo",
)


def _pretty_champ(name: str | None) -> str:
    text = str(name or "").replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else "Unknown"


def _parse_weave_stem(stem: str) -> dict[str, Any]:
    match = WEAVE_STEM_RE.match(stem)
    if not match:
        return {
            "gameIndex": None,
            "gameId": None,
            "champion": None,
            "opponentChampion": None,
            "result": None,
        }
    game_index = int(match.group("game"))
    return {
        "gameIndex": game_index,
        "gameId": f"g{game_index:02d}",
        "champion": _pretty_champ(match.group("champ")),
        "opponentChampion": _pretty_champ(match.group("opp")) if match.group("opp") else None,
        "result": (match.group("result") or "").lower() or None,
    }


def _weave_stem_for(path: Path, *, kind: str) -> str:
    stem = path.stem
    if kind == "music" and stem.endswith("_portrait_music"):
        return stem[: -len("_portrait_music")]
    if kind == "decorated" and stem.endswith("_portrait_decorated"):
        return stem[: -len("_portrait_decorated")]
    if kind == "portrait" and stem.endswith("_portrait"):
        return stem[: -len("_portrait")]
    return stem


def _is_picks_weave(path: Path) -> bool:
    if path.suffix.lower() != ".mp4" or not path.is_file():
        return False
    low = path.stem.lower()
    if any(bit in low for bit in SKIP_EXPORT_BITS):
        return False
    if low.endswith("_portrait") or "_portrait_" in low:
        return False
    return low.startswith("gam") or low.endswith("_weave")


def _is_dry_picks_portrait(path: Path) -> bool:
    if path.suffix.lower() != ".mp4" or not path.is_file():
        return False
    if "pool_samples" in path.parts:
        return False
    low = path.stem.lower()
    if not low.endswith("_portrait"):
        return False
    return not any(bit in low for bit in SKIP_EXPORT_BITS)


def _is_decorated_portrait(path: Path) -> bool:
    if path.suffix.lower() != ".mp4" or not path.is_file():
        return False
    if "pool_samples" in path.parts:
        return False
    return path.stem.lower().endswith("_portrait_decorated")


def _is_music_portrait(path: Path) -> bool:
    if path.suffix.lower() != ".mp4" or not path.is_file():
        return False
    if "pool_samples" in path.parts:
        return False
    return path.stem.lower().endswith("_portrait_music")


def _music_sidecar(path: Path) -> dict[str, Any]:
    sidecar = path.with_suffix(".json")
    if not sidecar.is_file():
        return {}
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _post_sidecar(path: Path) -> dict[str, Any]:
    """Upload metadata + platform results written by post_short.py."""
    sidecar = path.with_name(f"{path.stem}.post.json")
    if not sidecar.is_file():
        return {}
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def post_is_done(post: dict[str, Any] | None) -> bool:
    if not isinstance(post, dict):
        return False
    youtube = post.get("youtube") if isinstance(post.get("youtube"), dict) else {}
    tiktok = post.get("tiktok") if isinstance(post.get("tiktok"), dict) else {}
    return bool(youtube.get("videoId") or tiktok.get("publishId"))


def posting_status() -> dict[str, Any]:
    """Whether YouTube / TikTok credentials and tokens are in place."""
    try:
        from posting import tiktok, youtube

        return {"youtube": youtube.status(), "tiktok": tiktok.status()}
    except Exception:
        blank = {"configured": False, "authorized": False}
        return {"youtube": {"platform": "youtube", **blank}, "tiktok": {"platform": "tiktok", **blank}}


def _weave_report(picks_dir: Path) -> dict[str, dict[str, Any]]:
    path = picks_dir / "compilations.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in payload.get("weaves") or []:
        name = str(row.get("filename") or "")
        if name:
            out[name] = row
    return out


def _export_item(
    path: Path,
    *,
    dataset: Path,
    vid: str,
    day: str,
    kind: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rel = path.relative_to(dataset).as_posix()
    version = _file_version(path)
    urls = _media_urls(vid, day, rel, version=version)
    preview_jpg = path.with_name(f"{path.stem}_preview.jpg")
    thumb_url = urls["thumbUrl"]
    if preview_jpg.is_file():
        thumb_rel = preview_jpg.relative_to(dataset).as_posix()
        thumb_url = _media_urls(vid, day, thumb_rel, version=_file_version(preview_jpg))["mediaUrl"]
    weave_stem = _weave_stem_for(path, kind=kind)
    parsed = _parse_weave_stem(weave_stem)
    title_bits = [parsed.get("champion") or weave_stem]
    if parsed.get("opponentChampion"):
        title_bits.append(f"vs {parsed['opponentChampion']}")
    if parsed.get("result"):
        title_bits.append(parsed["result"])
    row = {
        "id": path.stem,
        "kind": kind,
        "weaveStem": weave_stem,
        "vodId": vid,
        "dayKey": day,
        "gameId": parsed.get("gameId"),
        "gameIndex": parsed.get("gameIndex"),
        "champion": parsed.get("champion"),
        "opponentChampion": parsed.get("opponentChampion"),
        "result": parsed.get("result"),
        "title": " · ".join(title_bits),
        "filename": path.name,
        "relativePath": rel,
        "mediaUrl": urls["mediaUrl"],
        "thumbUrl": thumb_url,
        "previewUrl": None,
        "local": True,
        "gcs": False,
        "bytes": path.stat().st_size,
    }
    if extra:
        row.update(extra)
    return row


def list_picks_exports(
    dataset: Path | None,
    *,
    vid: str,
    day: str,
    clips: list[dict[str, Any]],
    selections: dict[str, Any],
) -> dict[str, Any]:
    pick_games: list[str] = []
    seen: set[str] = set()
    for clip in clips:
        rec = selections.get(clip["id"]) or {}
        rating = rec.get("rating") or clip.get("rating")
        game_id = clip.get("gameId")
        if rating in {"godly", "excellent"} and game_id and game_id not in seen:
            seen.add(game_id)
            pick_games.append(game_id)
    pick_games.sort()
    empty: dict[str, Any] = {
        "weaves": [],
        "portraits": [],
        "decorated": [],
        "music": [],
        "tracks": [],
        "pickGames": pick_games,
    }
    if dataset is None:
        return empty
    picks_dir = dataset / "lol_compilations_picks"
    portrait_dir = dataset / "lol_compilations_picks_portrait"
    report = _weave_report(picks_dir) if picks_dir.is_dir() else {}
    weaves: list[dict[str, Any]] = []
    if picks_dir.is_dir():
        for path in sorted(picks_dir.glob("*.mp4")):
            if not _is_picks_weave(path):
                continue
            info = report.get(path.name) or {}
            weaves.append(
                _export_item(
                    path,
                    dataset=dataset,
                    vid=vid,
                    day=day,
                    kind="weave",
                    extra={"clipCount": info.get("clipCount"), "win": info.get("win")},
                )
            )
    portraits: list[dict[str, Any]] = []
    decorated: list[dict[str, Any]] = []
    music: list[dict[str, Any]] = []
    if portrait_dir.is_dir():
        music_paths = sorted(portrait_dir.glob("*.mp4")) + sorted((portrait_dir / "post").glob("*.mp4"))
        for path in sorted(portrait_dir.glob("*.mp4")):
            if _is_dry_picks_portrait(path):
                portraits.append(
                    _export_item(path, dataset=dataset, vid=vid, day=day, kind="portrait")
                )
            elif _is_decorated_portrait(path):
                decorated.append(
                    _export_item(path, dataset=dataset, vid=vid, day=day, kind="decorated")
                )
        seen_music: set[Path] = set()
        for path in music_paths:
            if not _is_music_portrait(path):
                continue
            key = path.resolve()
            if key in seen_music:
                continue
            seen_music.add(key)
            sidecar = _music_sidecar(path)
            music.append(
                _export_item(
                    path,
                    dataset=dataset,
                    vid=vid,
                    day=day,
                    kind="music",
                    extra={
                        "trackId": sidecar.get("track"),
                        "trackName": sidecar.get("name"),
                        "post": _post_sidecar(path) or None,
                    },
                )
            )
    try:
        from music_pool import list_tracks_public

        tracks = list_tracks_public()
    except Exception:
        tracks = []
    return {
        "weaves": weaves,
        "portraits": portraits,
        "decorated": decorated,
        "music": music,
        "tracks": tracks,
        "pickGames": pick_games,
    }


def find_export(exports: dict[str, Any], export_id: str) -> dict[str, Any] | None:
    needle = export_id.strip()
    if not needle:
        return None
    for kind in ("decorated", "music", "portraits", "weaves"):
        for row in exports.get(kind) or []:
            if row.get("id") == needle or row.get("weaveStem") == needle:
                return row
    return None


def find_music_export(exports: dict[str, Any], export_id: str) -> dict[str, Any] | None:
    """Only the music mix is postable, so resolve ids against that list."""
    needle = export_id.strip()
    if not needle:
        return None
    for row in exports.get("music") or []:
        if row.get("id") == needle or row.get("weaveStem") == needle:
            return row
    return None


def weave_report_for_dataset(dataset: Path | None) -> dict[str, dict[str, Any]]:
    if dataset is None:
        return {}
    picks_dir = dataset / "lol_compilations_picks"
    return _weave_report(picks_dir) if picks_dir.is_dir() else {}


def build_review(vod_id: str, *, day_key: str | None = None) -> dict[str, Any]:
    vid = vod_id.strip().lstrip("v")
    day = resolve_day(vid, day_key)
    dataset = find_dataset_dir(DATA, vid)
    payload, clips_dir, origin = _load_manifest(vid, day, dataset)
    items = list(payload.get("clips") or [])
    if not items and dataset is not None:
        items = _scan_local_clips(dataset / clips_dir)
        origin = origin or "local"
    clips = [
        row
        for row in (
            _normalize_clip(item, vid=vid, day=day, clips_dir=clips_dir, dataset=dataset)
            for item in items
        )
        if row is not None
    ]
    clips.sort(key=lambda c: (c["gameIndex"], c["clipIndexInGame"], c["id"]))
    selections = get_clip_selections(vid, day)
    classifications = get_classifications(vid, day)
    titles = get_titles(vid, day)
    for clip in clips:
        rec = selections.get(clip["id"]) or {}
        clip["rating"] = rec.get("rating")
        clip["reviewedAt"] = rec.get("reviewed_at")
        clip["classification"] = normalize_classification_entry(classifications.get(clip["id"]))
    games = sorted({c["gameId"] for c in clips})
    events = sorted({t.lower() for c in clips for t in c["types"]})
    exports = list_picks_exports(dataset, vid=vid, day=day, clips=clips, selections=selections)
    return {
        "vodId": vid,
        "dayKey": day,
        "title": _title(dataset, payload),
        "clipsDir": clips_dir,
        "origin": origin,
        "clips": clips,
        "selections": selections,
        "classifications": classifications,
        "titles": titles,
        "summary": summarize(clips, selections, classifications, exports=exports, titles=titles),
        "games": games,
        "events": events,
        "gcsReady": catalog.gcs_ready(),
        "exports": exports,
        "posting": posting_status(),
    }


def queue_row(clip: dict[str, Any], *, rating: str, reviewed_at: str | None) -> dict[str, Any]:
    return {
        "id": clip["id"],
        "rating": rating,
        "reviewed_at": reviewed_at,
        "vodId": clip.get("vodId"),
        "dayKey": clip.get("dayKey"),
        "gameId": clip.get("gameId"),
        "champion": clip.get("champion"),
        "event": clip.get("event"),
        "types": clip.get("types") or [],
        "gameTime": clip.get("gameTime"),
        "vodTimeSeconds": clip.get("vodTimeSeconds"),
        "relativePath": clip.get("relativePath"),
        "mediaUrl": clip.get("mediaUrl"),
    }


def _load_manifest(
    vid: str,
    day: str,
    dataset: Path | None,
) -> tuple[dict[str, Any], str, str]:
    local = _local_manifest(dataset) if dataset is not None else None
    if local is not None:
        return local
    remote = _gcs_manifest(vid, day)
    if remote is not None:
        return remote
    if dataset is not None and (dataset / "lol_clips").is_dir():
        return {}, "lol_clips", "local"
    raise FileNotFoundError(f"no lol_clips/clips.json for {vid}")


def _local_manifest(dataset: Path) -> tuple[dict[str, Any], str, str] | None:
    canonical = dataset / "lol_clips" / "clips.json"
    if canonical.is_file():
        return _read_json(canonical), "lol_clips", "local"
    versioned = sorted(dataset.glob("lol_clips_*/clips.json"))
    if not versioned:
        return None
    path = versioned[-1]
    return _read_json(path), path.parent.name, "local"


def _gcs_manifest(vid: str, day: str) -> tuple[dict[str, Any], str, str] | None:
    if not catalog.gcs_ready():
        return None
    try:
        import storage_gcs as gcs
    except Exception:
        return None
    object_name = catalog.gcs_object_name(vid, "lol_clips/clips.json", day_key=day)
    if not object_name:
        return None
    try:
        payload = gcs.read_json_blob(object_name)
    except FileNotFoundError:
        return None
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return payload, "lol_clips", "gcs"


def _scan_local_clips(clips_dir: Path) -> list[dict[str, Any]]:
    if not clips_dir.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(clips_dir.rglob("c*.mp4")):
        if not path.is_file() or ".preview." in path.name or ".trim.tmp." in path.name:
            continue
        if "_archived" in path.parts:
            continue
        folder = FOLDER_RE.match(path.parent.name)
        stem = STEM_RE.match(path.stem)
        if not folder or not stem:
            continue
        types = [part.strip().upper() for part in str(stem.group("label") or "").split("+") if part.strip()]
        rows.append(
            {
                "gameIndex": int(folder.group("game")),
                "clipIndexInGame": int(stem.group("idx")),
                "relativePath": path.relative_to(clips_dir).as_posix(),
                "filename": path.name,
                "champion": folder.group("champ"),
                "laneOpponentChampion": folder.group("lane"),
                "opponentChampion": stem.group("opp"),
                "types": types,
            }
        )
    return rows


def _normalize_clip(
    item: dict[str, Any],
    *,
    vid: str,
    day: str,
    clips_dir: str,
    dataset: Path | None,
) -> dict[str, Any] | None:
    rel = str(item.get("relativePath") or item.get("filename") or "").lstrip("/")
    if not rel:
        return None
    filename = Path(rel).name
    folder_name = Path(rel).parent.name
    folder = FOLDER_RE.match(folder_name) if folder_name else None
    stem = STEM_RE.match(Path(filename).stem)
    game_index = item.get("gameIndex") or (int(folder.group("game")) if folder else None)
    clip_index = item.get("clipIndexInGame") or (int(stem.group("idx")) if stem else None)
    if game_index is None or clip_index is None:
        return None
    types = [str(t).strip().upper() for t in (item.get("types") or []) if str(t).strip()]
    if not types and stem:
        types = [part.strip().upper() for part in str(stem.group("label") or "").split("+") if part.strip()]
    dataset_rel = f"{clips_dir}/{rel}"
    local_path = dataset / dataset_rel if dataset is not None else None
    local_file = local_path is not None and local_path.is_file()
    preview_rel = _preview_rel(dataset, dataset_rel)
    version = _file_version(local_path) if local_file else None
    urls = _media_urls(vid, day, dataset_rel, version=version)
    preview_urls = _media_urls(vid, day, preview_rel, version=version) if preview_rel else {}
    start = item.get("start") or item.get("windowLocalStart") or item.get("localStart")
    try:
        vod_seconds = float(start) if start is not None else None
    except (TypeError, ValueError):
        vod_seconds = None
    return {
        "id": clip_id(int(game_index), int(clip_index)),
        "vodId": vid,
        "dayKey": day,
        "gameId": f"g{int(game_index):02d}",
        "gameIndex": int(game_index),
        "clipIndexInGame": int(clip_index),
        "champion": item.get("champion") or (folder.group("champ") if folder else None) or "Unknown",
        "event": "+".join(t.lower() for t in types) if types else "event",
        "types": types,
        "gameTime": item.get("gameTime") or "",
        "vodTime": item.get("vodTime") or "",
        "vodTimeSeconds": vod_seconds,
        "duration": item.get("duration"),
        "opponentChampion": item.get("opponentChampion") or (stem.group("opp") if stem else None),
        "laneOpponentChampion": item.get("laneOpponentChampion")
        or (folder.group("lane") if folder else None),
        "win": item.get("win"),
        "relativePath": dataset_rel,
        "filename": filename,
        "previewPath": preview_rel,
        "portraitPath": None,
        "mediaUrl": urls["mediaUrl"],
        "thumbUrl": urls["thumbUrl"] if local_file else None,
        "previewUrl": preview_urls.get("mediaUrl"),
        "portraitUrl": None,
        "local": bool(local_file),
        "gcs": not local_file,
        "trimmed": bool(_archived_versions(dataset, dataset_rel)),
    }


def trim_clip_local(
    vod_id: str,
    *,
    day_key: str | None,
    relative_path: str,
    start: float,
    end: float,
) -> dict[str, Any]:
    """Cut [start, end) from a local clip, archive the previous file, replace in place."""
    start = max(0.0, float(start))
    end = float(end)
    if end - start < 0.3:
        raise ValueError("clip must be at least 0.3 seconds")

    dest_dir = _dataset_dir(vod_id, day_key)
    rel = relative_path.lstrip("/")
    src = dest_dir / rel
    if dest_dir.resolve() not in src.resolve().parents:
        raise ValueError("path escapes dataset dir")
    if not src.is_file():
        src = pull_clip_local(vod_id, day_key=day_key, relative_path=rel)
    duration = _probe_duration(src)
    if duration is not None:
        end = min(end, duration)
    if end - start < 0.3:
        raise ValueError("clip must be at least 0.3 seconds")

    archived = _archive_original(src, dest_dir, rel)
    tmp = src.with_name(src.stem + ".trim.tmp.mp4")
    try:
        _ffmpeg_trim(src, tmp, start, end)
        tmp.replace(src)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return {
        "relativePath": rel,
        "archivedPath": archived.relative_to(dest_dir).as_posix(),
        "start": round(start, 3),
        "end": round(end, 3),
        "duration": round(end - start, 3),
    }


def uncut_clip_local(
    vod_id: str,
    *,
    day_key: str | None,
    relative_path: str,
) -> dict[str, Any]:
    """Replace the live clip with the oldest archived original."""
    dest_dir = _dataset_dir(vod_id, day_key)
    rel = relative_path.lstrip("/")
    src = dest_dir / rel
    if dest_dir.resolve() not in src.resolve().parents:
        raise ValueError("path escapes dataset dir")
    versions = _archived_versions(dest_dir, rel)
    if not versions:
        raise FileNotFoundError("no archived original for this clip")
    original = versions[0]
    if src.is_file():
        live_size = src.stat().st_size
        if live_size != original.stat().st_size:
            _archive_original(src, dest_dir, rel)
    src.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(original, src)
    return {
        "relativePath": rel,
        "restoredPath": original.relative_to(dest_dir).as_posix(),
        "bytes": src.stat().st_size,
    }


def _archive_original(src: Path, dest_dir: Path, rel: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rel_path = Path(rel)
    archived = dest_dir / "lol_clips" / "_archived" / rel_path.parent.name / f"{src.stem}__{stamp}{src.suffix}"
    archived.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, archived)
    return archived


def _ffmpeg_trim(src: Path, dest: Path, start: float, end: float) -> None:
    from ffmpeg_color import VIDEO_TO_BT709, X264_BT709

    duration = max(0.3, end - start)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{duration:.3f}",
        "-vf",
        VIDEO_TO_BT709,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        *X264_BT709,
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size < 1000:
        err = (proc.stderr or proc.stdout or "ffmpeg trim failed").strip()
        raise RuntimeError(err.splitlines()[-1] if err else "ffmpeg trim failed")


def _probe_duration(path: Path) -> float | None:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    try:
        return float(proc.stdout.strip())
    except (TypeError, ValueError):
        return None


def _archived_versions(dataset: Path | None, dataset_rel: str) -> list[Path]:
    if dataset is None:
        return []
    rel = Path(dataset_rel)
    folder = dataset / "lol_clips" / "_archived" / rel.parent.name
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob(f"{rel.stem}__*.mp4") if p.is_file())


def _is_trimmed(dataset: Path | None, dataset_rel: str) -> bool:
    return bool(_archived_versions(dataset, dataset_rel))


def _file_version(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    st = path.stat()
    return f"{int(st.st_mtime)}-{st.st_size}"


def pull_clip_local(vod_id: str, *, day_key: str | None, relative_path: str) -> Path:
    result = pull_clips_local(vod_id, day_key=day_key, items=[("", relative_path)])
    failed = result.get("failed") or []
    if failed:
        raise RuntimeError(failed[0].get("error") or "download failed")
    dest_dir = _dataset_dir(vod_id, day_key)
    dest = dest_dir / relative_path.lstrip("/")
    if not dest.is_file():
        raise FileNotFoundError(relative_path)
    return dest


def pull_clips_local(
    vod_id: str,
    *,
    day_key: str | None,
    items: list[tuple[str, str]],
    workers: int = 8,
) -> dict[str, Any]:
    """Download many clips from GCS in parallel. items are (clip_id, relative_path)."""
    pulled: list[str] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []
    for event in iter_pull_clips(vod_id, day_key=day_key, items=items, workers=workers):
        if event.get("event") == "progress":
            clip_id = event.get("id")
            if clip_id and event.get("ok"):
                pulled.append(clip_id)
            elif clip_id and not event.get("ok"):
                failed.append({"id": clip_id, "error": event.get("error") or "download failed"})
            elif event.get("skipped") and not clip_id:
                skipped.extend([""] * int(event["skipped"]))
        elif event.get("event") == "done":
            if not skipped and event.get("skipped"):
                skipped.extend([""] * int(event["skipped"]))
    return {"pulled": pulled, "skipped": skipped, "failed": failed}


def iter_pull_clips(
    vod_id: str,
    *,
    day_key: str | None,
    items: list[tuple[str, str]],
    workers: int = 8,
    cancel: threading.Event | None = None,
):
    """Yield progress dicts while downloading in parallel."""
    if not catalog.gcs_ready():
        raise RuntimeError("GCS is not configured")
    import storage_gcs as gcs
    from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed

    stop = cancel or threading.Event()
    vid = vod_id.strip().lstrip("v")
    day = resolve_day(vid, day_key)
    dest_dir = _dataset_dir(vid, day)
    dest_dir.mkdir(parents=True, exist_ok=True)
    base = f"{gcs.prefix()}/{day}/{vid}"
    client = gcs._client()
    bucket = client.bucket(gcs.bucket_name())
    _ensure_local_clips_json(dest_dir, base, bucket)

    pending: list[tuple[str, str, Path]] = []
    skipped = 0
    for clip_id, rel in items:
        rel = rel.lstrip("/")
        dest = dest_dir / rel
        if dest_dir.resolve() not in dest.resolve().parents and dest.resolve() != dest_dir.resolve():
            raise ValueError("path escapes dataset dir")
        if dest.is_file() and dest.stat().st_size > 10_000:
            skipped += 1
            continue
        pending.append((clip_id, rel, dest))

    total = len(pending)
    done = 0
    failed = 0
    cancelled = 0
    if skipped:
        yield {"event": "progress", "done": 0, "total": total, "skipped": skipped, "id": None, "ok": True}

    def one(job: tuple[str, str, Path]) -> str:
        clip_id, rel, dest = job
        if stop.is_set():
            raise CancelledError()
        dest.parent.mkdir(parents=True, exist_ok=True)
        bucket.blob(f"{base}/{rel}").download_to_filename(str(dest))
        return clip_id

    pool = None
    try:
        if pending:
            workers = max(1, min(workers, len(pending)))
            pool = ThreadPoolExecutor(max_workers=workers)
            futs = {pool.submit(one, job): job[0] for job in pending}
            for fut in as_completed(futs):
                if stop.is_set():
                    break
                clip_id = futs[fut]
                ok = True
                err = None
                try:
                    fut.result()
                except CancelledError:
                    cancelled += 1
                    continue
                except Exception as exc:
                    ok = False
                    failed += 1
                    err = str(exc)
                done += 1
                yield {
                    "event": "progress",
                    "done": done,
                    "total": total,
                    "id": clip_id,
                    "ok": ok,
                    "error": err,
                }
    except GeneratorExit:
        stop.set()
        raise
    finally:
        if pool is not None:
            stop.set()
            pool.shutdown(wait=False, cancel_futures=True)
    yield {
        "event": "done",
        "done": done,
        "total": total,
        "failed": failed,
        "skipped": skipped,
        "cancelled": stop.is_set(),
        "dropped": cancelled,
    }


def reveal_local_path(path: Path) -> str:
    """Reveal a local file in Finder, Explorer, or the default file manager."""
    if not path.is_file():
        raise FileNotFoundError(str(path))
    resolved = path.resolve()
    if sys.platform == "darwin":
        resolved_text = str(resolved).replace("\\", "\\\\").replace('"', '\\"')
        script = (
            f'tell application "Finder" to reveal POSIX file "{resolved_text}"\n'
            'tell application "Finder" to activate'
        )
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        if proc.returncode != 0:
            proc = subprocess.run(["open", "-R", str(resolved)], capture_output=True, text=True)
        label = "Finder"
    elif sys.platform == "win32":
        proc = subprocess.run(["explorer", "/select,", str(resolved)], capture_output=True, text=True)
        label = "Explorer"
    else:
        proc = subprocess.run(["xdg-open", str(resolved.parent)], capture_output=True, text=True)
        label = "file manager"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "reveal failed").strip()
        raise RuntimeError(err.splitlines()[-1] if err else "reveal failed")
    return label


def find_review_item(payload: dict[str, Any], item_id: str) -> dict[str, Any] | None:
    for clip in payload.get("clips") or []:
        if clip.get("id") == item_id:
            return clip
    exports = payload.get("exports") or {}
    for key in ("weaves", "portraits", "decorated", "music"):
        for row in exports.get(key) or []:
            if row.get("id") == item_id:
                return row
    return None


def reveal_clip_local(
    vod_id: str,
    *,
    day_key: str | None,
    relative_path: str,
) -> dict[str, str]:
    dest_dir = _dataset_dir(vod_id, day_key)
    rel = relative_path.lstrip("/")
    path = dest_dir / rel
    if dest_dir.resolve() not in path.resolve().parents and path.resolve() != dest_dir.resolve():
        raise ValueError("path escapes dataset dir")
    label = reveal_local_path(path)
    return {"path": str(path.resolve()), "explorer": label}


def _dataset_dir(vod_id: str, day_key: str | None) -> Path:
    vid = vod_id.strip().lstrip("v")
    day = resolve_day(vid, day_key)
    dest_dir = find_dataset_dir(DATA, vid)
    if dest_dir is None:
        dest_dir = resolve_dataset_dir(DATA, vid, day_key=None if day == "local" else day)
    return dest_dir


def _ensure_local_clips_json(dest_dir: Path, base: str, bucket: Any) -> None:
    dest = dest_dir / "lol_clips" / "clips.json"
    if dest.is_file():
        return
    blob = bucket.blob(f"{base}/lol_clips/clips.json")
    if not blob.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    blob.download_to_filename(str(dest))


def _preview_rel(dataset: Path | None, dataset_rel: str) -> str | None:
    if not dataset_rel.endswith(".mp4"):
        return None
    preview = dataset_rel[:-4] + ".preview.mp4"
    if dataset is not None and (dataset / preview).is_file():
        return preview
    return None


def _media_urls(vid: str, day: str, rel: str, *, version: str | None = None) -> dict[str, str]:
    query = {"vod": vid, "path": rel, "src": "auto"}
    if day and day != "local":
        query["day"] = day
    if version:
        query["v"] = version
    encoded = urlencode(query)
    return {
        "mediaUrl": f"/api/media?{encoded}",
        "thumbUrl": f"/api/thumb?{encoded}",
    }


def _title(dataset: Path | None, payload: dict[str, Any]) -> str | None:
    if dataset is not None:
        for name in ("metadata.json", "source.info.json", "archive_manifest.json"):
            path = dataset / name
            if not path.is_file():
                continue
            raw = _read_json(path)
            title = (
                raw.get("title")
                or raw.get("fulltitle")
                or (raw.get("twitch") or {}).get("title")
            )
            if title:
                return str(title)
    return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
