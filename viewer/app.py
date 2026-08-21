#!/usr/bin/env python3
"""Local bucket viewer: browse processed videos, requeue jobs, keep/skip."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from dataset_paths import find_dataset_dir
from env_loader import load_dotenv

from viewer import catalog, jobs, review, store

import classify_clip
from clip_classifiers import wrap_record
from posting import meta as post_meta
from title_suggestions import TitleSuggestionProvider, build_title_context

load_dotenv()
os.environ.setdefault("GCS_BUCKET", "poststream-assistant-archive")
os.environ.setdefault("GCS_PREFIX", "vods")

STATIC = Path(__file__).resolve().parent / "static"
CACHE = ROOT / ".cache" / "viewer"
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

app = FastAPI(title="VOD archive viewer", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


class NotesBody(BaseModel):
    notes: str = ""


class ReviewBody(BaseModel):
    path: str
    status: str | None = None


class JobBody(BaseModel):
    job: str
    vodId: str = ""
    dayKey: str = ""
    extra: list[str] = Field(default_factory=list)
    where: str = "cloud"


class DeleteBody(BaseModel):
    path: str
    gcs: bool = False
    local: bool = True


class PullBody(BaseModel):
    path: str


class ClipSelectionBody(BaseModel):
    id: str
    rating: str | None = None


class ClipPullBody(BaseModel):
    id: str | None = None
    ids: list[str] = Field(default_factory=list)


class ClipTrimBody(BaseModel):
    id: str
    start: float
    end: float


class ClipUncutBody(BaseModel):
    id: str


class ClipRevealBody(BaseModel):
    id: str


class ClassifyBody(BaseModel):
    id: str
    mode: str = "rules"


class ClassificationReviewBody(BaseModel):
    id: str
    status: str
    hook_text: str | None = None
    source: str = "rules"


class TitleReviewBody(BaseModel):
    id: str
    status: str
    selected: str | None = None
    selected_style: str | None = None


class TitleGenerateBody(BaseModel):
    id: str
    context: str | None = None
    reprompt: str | None = None
    previous_hooks: list[str] = Field(default_factory=list)


class PostMetaBody(BaseModel):
    id: str
    title: str | None = None
    description: str | None = None
    hashtags: list[str] | None = None
    selected_style: str | None = None


def _error(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/review")
def review_index() -> FileResponse:
    return FileResponse(STATIC / "review.html")


@app.get("/review/{vod_id}")
def review_page(vod_id: str) -> FileResponse:
    return FileResponse(STATIC / "review.html")


@app.get("/review/{day_key}/{vod_id}")
def review_page_day(day_key: str, vod_id: str) -> FileResponse:
    return FileResponse(STATIC / "review.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "gcs": catalog.gcs_ready()}


@app.get("/api/catalog")
def api_catalog(gcs: bool = True) -> dict[str, Any]:
    return catalog.build_catalog(include_gcs=gcs)


@app.get("/api/vods/{vod_id}")
def api_vod(vod_id: str, day: str | None = None) -> dict[str, Any]:
    try:
        vod = catalog.get_vod(vod_id, day_key=day)
    except FileNotFoundError as exc:
        raise _error(404, str(exc)) from exc
    rec = store.get_vod(vod["vodId"])
    for video in vod.get("videos") or []:
        video["poster"] = catalog.poster_for(vod, video["path"])
        video["review"] = (rec.get("files") or {}).get(video["path"])
    vod["notes"] = rec.get("notes") or ""
    vod["reviews"] = rec.get("files") or {}
    vod["jobs"] = jobs.catalog()
    return vod


@app.get("/api/jobs")
def api_jobs() -> dict[str, Any]:
    return {"jobs": jobs.catalog()}


@app.get("/api/jobs/active")
def api_jobs_active(vod_id: str = "") -> dict[str, Any]:
    return {"job": jobs.active_job(vod_id)}


@app.post("/api/jobs")
def api_run_job(body: JobBody) -> JSONResponse:
    extra = [a for a in body.extra if a.strip()]
    where = body.where if body.where in {"cloud", "local"} else "cloud"
    try:
        preview = jobs.preview_command(
            body.job,
            vod_id=body.vodId,
            day_key=body.dayKey,
            extra=extra,
            where=where,
        )
        result = jobs.run_job(
            body.job,
            vod_id=body.vodId,
            day_key=body.dayKey,
            extra=extra,
            where=where,
        )
    except KeyError as exc:
        raise _error(404, str(exc)) from exc
    except ValueError as exc:
        raise _error(400, str(exc)) from exc
    result["preview"] = preview
    return JSONResponse(result)


@app.put("/api/vods/{vod_id}/notes")
def api_notes(vod_id: str, body: NotesBody) -> dict[str, Any]:
    return store.set_notes(vod_id.strip().lstrip("v"), body.notes)


@app.put("/api/vods/{vod_id}/review")
def api_review(vod_id: str, body: ReviewBody) -> dict[str, Any]:
    status = body.status or None
    if status == "clear":
        status = None
    try:
        return store.set_file_review(vod_id.strip().lstrip("v"), body.path, status)
    except ValueError as exc:
        raise _error(400, str(exc)) from exc


def _review_payload(vod_id: str, day: str | None) -> dict[str, Any]:
    try:
        payload = review.build_review(vod_id, day_key=day)
    except FileNotFoundError as exc:
        raise _error(404, str(exc)) from exc
    payload["activeJob"] = jobs.active_job(payload.get("vodId") or vod_id)
    return payload


@app.get("/api/review/{vod_id}")
def api_clip_review(vod_id: str, day: str | None = None) -> dict[str, Any]:
    return _review_payload(vod_id, day)


@app.get("/api/review/{vod_id}/selections")
@app.get("/api/jobs/{vod_id}/selections")
def api_clip_selections(vod_id: str, day: str | None = None) -> dict[str, Any]:
    payload = _review_payload(vod_id, day)
    return {"vodId": payload["vodId"], "dayKey": payload["dayKey"], "selections": payload["selections"]}


@app.put("/api/review/{vod_id}/selections")
@app.put("/api/jobs/{vod_id}/selections")
def api_set_clip_selection(vod_id: str, body: ClipSelectionBody, day: str | None = None) -> dict[str, Any]:
    payload = _review_payload(vod_id, day)
    vid = payload["vodId"]
    day_key = payload["dayKey"]
    clip_id = body.id.strip()
    known = {clip["id"] for clip in payload["clips"]}
    if clip_id not in known:
        raise _error(404, f"unknown clip {clip_id}")
    rating = body.rating or None
    if rating in {"clear", "unreviewed"}:
        rating = None
    try:
        selections = store.set_clip_selection(vid, day_key, clip_id, rating)
    except ValueError as exc:
        raise _error(400, str(exc)) from exc
    store.write_approved_queues(vid, day_key, payload["clips"], selections, review.queue_row)
    payload["selections"] = selections
    payload["summary"] = review.summarize(
        payload["clips"],
        selections,
        payload.get("classifications"),
        exports=payload.get("exports"),
        titles=payload.get("titles"),
    )
    for clip in payload["clips"]:
        rec = selections.get(clip["id"]) or {}
        clip["rating"] = rec.get("rating")
        clip["reviewedAt"] = rec.get("reviewed_at")
    dataset = find_dataset_dir(catalog.DATA, vid)
    payload["exports"] = review.list_picks_exports(
        dataset, vid=vid, day=day_key, clips=payload["clips"], selections=selections
    )
    return payload


@app.post("/api/review/{vod_id}/classify")
def api_classify_clip(vod_id: str, body: ClassifyBody, day: str | None = None) -> dict[str, Any]:
    payload = _review_payload(vod_id, day)
    clip_id = body.id.strip()
    clip = next((row for row in payload["clips"] if row["id"] == clip_id), None)
    if clip is None:
        raise _error(404, f"unknown clip {clip_id}")
    mode = body.mode.strip().lower()
    if mode not in {"rules", "ai", "hybrid"}:
        raise _error(400, "mode must be rules, ai, or hybrid")
    source = "ai" if mode == "ai" else "rules"
    dataset = find_dataset_dir(catalog.DATA, payload["vodId"])
    try:
        result = classify_clip.classify_clip(clip, dataset_dir=dataset, mode=mode)  # type: ignore[arg-type]
    except RuntimeError as exc:
        raise _error(503, str(exc)) from exc
    except Exception as exc:
        raise _error(500, f"classification failed: {exc}") from exc
    record = wrap_record(result, source=source)
    bundle = store.set_classification(payload["vodId"], payload["dayKey"], clip_id, record, source=source)
    clip["classification"] = bundle
    payload["classifications"] = store.get_classifications(payload["vodId"], payload["dayKey"])
    payload["summary"] = review.summarize(
        payload["clips"],
        payload["selections"],
        payload["classifications"],
        exports=payload.get("exports"),
        titles=payload.get("titles"),
    )
    return bundle


@app.put("/api/review/{vod_id}/classifications")
def api_review_classification(
    vod_id: str,
    body: ClassificationReviewBody,
    day: str | None = None,
) -> dict[str, Any]:
    payload = _review_payload(vod_id, day)
    clip_id = body.id.strip()
    known = {clip["id"] for clip in payload["clips"]}
    if clip_id not in known:
        raise _error(404, f"unknown clip {clip_id}")
    try:
        bundle = store.update_classification_review(
            payload["vodId"],
            payload["dayKey"],
            clip_id,
            status=body.status,
            hook_text=body.hook_text,
            source=body.source if body.source in store.CLASSIFICATION_SOURCES else "rules",
        )
    except ValueError as exc:
        raise _error(400, str(exc)) from exc
    for clip in payload["clips"]:
        if clip["id"] == clip_id:
            clip["classification"] = bundle
            break
    return bundle


@app.post("/api/review/{vod_id}/titles/generate")
def api_generate_titles(vod_id: str, body: TitleGenerateBody, day: str | None = None) -> dict[str, Any]:
    payload = _review_payload(vod_id, day)
    exports = payload.get("exports") or {}
    export = review.find_export(exports, body.id.strip())
    if export is None:
        raise _error(404, f"unknown export {body.id}")
    weave_stem = str(export.get("weaveStem") or "").strip()
    if not weave_stem:
        raise _error(400, "export has no weave stem")
    dataset = find_dataset_dir(catalog.DATA, payload["vodId"])
    bits: list[str] = []
    if (body.context or "").strip():
        bits.append(body.context.strip())
    if (body.reprompt or "").strip():
        bits.append(f"Refine: {body.reprompt.strip()}")
    prev = [str(h).strip() for h in (body.previous_hooks or []) if str(h).strip()]
    if prev:
        bits.append("Previous hooks (write different options): " + " | ".join(prev[:5]))
    user_context = "\n".join(bits) or None
    context = build_title_context(
        weave_stem=weave_stem,
        export=export,
        clips=payload["clips"],
        classifications=payload.get("classifications") or {},
        selections=payload.get("selections") or {},
        vod_title=payload.get("title"),
        dataset=dataset,
        weave_report=review.weave_report_for_dataset(dataset),
        user_context=user_context,
    )
    try:
        result = TitleSuggestionProvider().generate(context)
    except RuntimeError as exc:
        raise _error(503, str(exc)) from exc
    except Exception as exc:
        raise _error(500, f"title generation failed: {exc}") from exc
    record_payload: dict[str, Any] = {
        "suggestions": result["suggestions"],
        "hookOptions": result.get("hookOptions") or [],
        "hooks": result.get("hooks") or {},
        "bestHook": result.get("bestHook"),
        "bestReason": result.get("bestReason"),
        "hashtags": result.get("hashtags") or [],
        "selected": result.get("bestHook") or (result["suggestions"][0] if result["suggestions"] else ""),
        "selectedStyle": "best",
        "status": "pending",
        "source": "ai",
    }
    if user_context:
        record_payload["userContext"] = user_context
    record = store.set_title_record(
        payload["vodId"],
        payload["dayKey"],
        weave_stem,
        record_payload,
    )
    return record


@app.put("/api/review/{vod_id}/titles")
def api_review_title(vod_id: str, body: TitleReviewBody, day: str | None = None) -> dict[str, Any]:
    payload = _review_payload(vod_id, day)
    exports = payload.get("exports") or {}
    export = review.find_export(exports, body.id.strip())
    if export is None:
        raise _error(404, f"unknown export {body.id}")
    weave_stem = str(export.get("weaveStem") or "").strip()
    if not weave_stem:
        raise _error(400, "export has no weave stem")
    try:
        record = store.update_title_review(
            payload["vodId"],
            payload["dayKey"],
            weave_stem,
            status=body.status,
            selected=body.selected,
            selected_style=body.selected_style,
        )
    except ValueError as exc:
        raise _error(400, str(exc)) from exc
    return record


@app.put("/api/review/{vod_id}/post/meta")
def api_post_meta(vod_id: str, body: PostMetaBody, day: str | None = None) -> dict[str, Any]:
    """Write the .post.json sidecar next to a music mix, before uploading it."""
    payload = _review_payload(vod_id, day)
    export = review.find_music_export(payload.get("exports") or {}, body.id.strip())
    if export is None:
        raise _error(404, f"unknown music export {body.id}")
    try:
        video = catalog.resolve_local(payload["vodId"], str(export.get("relativePath") or ""))
    except (FileNotFoundError, ValueError) as exc:
        raise _error(404, f"no local file for {body.id}") from exc
    dataset = find_dataset_dir(catalog.DATA, payload["vodId"])
    tags = [str(tag).lstrip("#").strip() for tag in (body.hashtags or []) if str(tag).strip()]
    record = post_meta.build_record(
        video,
        dataset=dataset,
        title=body.title,
        description=body.description,
        hashtags=tags or None,
    )
    post_meta.write_sidecar(video, record)
    return record


@app.post("/api/review/{vod_id}/pull")
def api_review_pull(vod_id: str, body: ClipPullBody, day: str | None = None):
    payload = _review_payload(vod_id, day)
    ids: list[str] = []
    seen: set[str] = set()
    for raw in list(body.ids or []) + ([body.id] if body.id else []):
        clip_id = str(raw).strip()
        if not clip_id or clip_id in seen:
            continue
        seen.add(clip_id)
        ids.append(clip_id)
    if not ids:
        raise _error(400, "id or ids required")

    by_id = {row["id"]: row for row in payload["clips"]}
    items: list[tuple[str, str]] = []
    for clip_id in ids:
        clip = by_id.get(clip_id)
        if clip is None:
            raise _error(404, f"unknown clip {clip_id}")
        if clip.get("local"):
            continue
        items.append((clip_id, clip["relativePath"]))

    def events():
        cancel = threading.Event()
        try:
            for event in review.iter_pull_clips(
                payload["vodId"],
                day_key=payload["dayKey"],
                items=items,
                cancel=cancel,
            ):
                yield json.dumps(event) + "\n"
        except GeneratorExit:
            cancel.set()
            raise
        except Exception as exc:
            yield json.dumps({"event": "error", "error": str(exc)}) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


@app.post("/api/review/{vod_id}/trim")
def api_review_trim(vod_id: str, body: ClipTrimBody, day: str | None = None) -> dict[str, Any]:
    payload = _review_payload(vod_id, day)
    clip_id = body.id.strip()
    clip = next((row for row in payload["clips"] if row["id"] == clip_id), None)
    if clip is None:
        raise _error(404, f"unknown clip {clip_id}")
    try:
        review.trim_clip_local(
            payload["vodId"],
            day_key=payload["dayKey"],
            relative_path=clip["relativePath"],
            start=body.start,
            end=body.end,
        )
    except FileNotFoundError as exc:
        raise _error(404, str(exc)) from exc
    except ValueError as exc:
        raise _error(400, str(exc)) from exc
    except RuntimeError as exc:
        raise _error(400, str(exc)) from exc
    return _review_payload(payload["vodId"], payload["dayKey"])


@app.post("/api/review/{vod_id}/uncut")
def api_review_uncut(vod_id: str, body: ClipUncutBody, day: str | None = None) -> dict[str, Any]:
    payload = _review_payload(vod_id, day)
    clip_id = body.id.strip()
    clip = next((row for row in payload["clips"] if row["id"] == clip_id), None)
    if clip is None:
        raise _error(404, f"unknown clip {clip_id}")
    try:
        review.uncut_clip_local(
            payload["vodId"],
            day_key=payload["dayKey"],
            relative_path=clip["relativePath"],
        )
    except FileNotFoundError as exc:
        raise _error(404, str(exc)) from exc
    except ValueError as exc:
        raise _error(400, str(exc)) from exc
    except RuntimeError as exc:
        raise _error(400, str(exc)) from exc
    return _review_payload(payload["vodId"], payload["dayKey"])


@app.post("/api/review/{vod_id}/reveal")
def api_review_reveal(vod_id: str, body: ClipRevealBody, day: str | None = None) -> dict[str, Any]:
    payload = _review_payload(vod_id, day)
    item_id = body.id.strip()
    item = review.find_review_item(payload, item_id)
    if item is None:
        raise _error(404, f"unknown item {item_id}")
    if not item.get("local"):
        raise _error(400, "item is not on disk")
    rel = item.get("relativePath")
    if not rel:
        raise _error(400, "item has no local path")
    try:
        return review.reveal_clip_local(
            payload["vodId"],
            day_key=payload["dayKey"],
            relative_path=str(rel),
        ) | {"ok": True}
    except FileNotFoundError as exc:
        raise _error(404, str(exc)) from exc
    except ValueError as exc:
        raise _error(400, str(exc)) from exc
    except RuntimeError as exc:
        raise _error(500, str(exc)) from exc


@app.post("/api/vods/{vod_id}/delete")
def api_delete(vod_id: str, body: DeleteBody, day: str | None = None) -> dict[str, Any]:
    vid = vod_id.strip().lstrip("v")
    removed: dict[str, bool] = {"local": False, "gcs": False}
    if body.local:
        try:
            path = catalog.resolve_local(vid, body.path)
            path.unlink()
            removed["local"] = True
        except FileNotFoundError:
            pass
        except ValueError as exc:
            raise _error(400, str(exc)) from exc
    if body.gcs:
        if not catalog.gcs_ready():
            raise _error(400, "GCS is not configured")
        import storage_gcs as gcs

        vod = catalog.get_vod(vid, day_key=day)
        prefix = vod.get("gcsPrefix")
        if not prefix:
            day_key = vod.get("dayKey")
            if not day_key or day_key == "local":
                raise _error(400, "no GCS prefix for this VOD")
            prefix = f"{gcs.prefix()}/{day_key}/{vid}"
        removed["gcs"] = gcs.delete_object(f"{prefix}/{body.path}")
    return {"ok": True, "removed": removed, "path": body.path}


@app.post("/api/vods/{vod_id}/pull")
def api_pull(vod_id: str, body: PullBody, day: str | None = None) -> dict[str, Any]:
    if not catalog.gcs_ready():
        raise _error(400, "GCS is not configured")
    import storage_gcs as gcs

    vid = vod_id.strip().lstrip("v")
    vod = catalog.get_vod(vid, day_key=day)
    prefix = vod.get("gcsPrefix")
    if not prefix:
        raise _error(400, "no GCS prefix for this VOD")
    dest = ROOT / "data" / vid / body.path
    dest.parent.mkdir(parents=True, exist_ok=True)
    gcs.download_file(f"{prefix}/{body.path}", dest)
    return {"ok": True, "path": str(dest), "bytes": dest.stat().st_size}


_THUMB_CACHE_HEADERS = {"Cache-Control": "private, max-age=86400"}
_MEDIA_CACHE_HEADERS = {"Cache-Control": "private, no-cache"}


@app.get("/api/media")
def api_media(
    request: Request,
    vod: str = Query(...),
    path: str = Query(...),
    src: str = Query("auto"),
    day: str | None = None,
) -> Response:
    rel = unquote(path).lstrip("/")
    vid = vod.strip().lstrip("v")
    if src in {"auto", "local"}:
        try:
            local = catalog.resolve_local(vid, rel)
            return FileResponse(
                local,
                media_type=mimetypes.guess_type(local.name)[0] or "video/mp4",
                filename=local.name,
                headers=_MEDIA_CACHE_HEADERS,
            )
        except (FileNotFoundError, ValueError):
            if src == "local":
                raise _error(404, rel)
    if src in {"auto", "gcs"}:
        if Path(rel).name.startswith("source."):
            raise _error(
                400,
                "Refusing to stream source.* from GCS (Coldline). Use a processed mp4.",
            )
        return _gcs_media(request, vid, rel, day=day)
    raise _error(404, rel)


@app.get("/api/music/preview")
def api_music_preview(track: str = Query(...)) -> FileResponse:
    from music_pool import track_by_id, track_file, track_ready

    row = track_by_id(track.strip())
    if row is None:
        raise _error(404, f"unknown track {track}")
    if not track_ready(row):
        raise _error(404, "track file missing")
    path = track_file(row)
    mime = mimetypes.guess_type(path.name)[0] or "audio/mpeg"
    return FileResponse(
        path,
        media_type=mime,
        filename=path.name,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@app.get("/api/thumb")
def api_thumb(
    request: Request,
    vod: str,
    path: str,
    src: str = "auto",
    day: str | None = None,
) -> Response:
    rel = unquote(path).lstrip("/")
    vid = vod.strip().lstrip("v")
    try:
        local = catalog.resolve_local(vid, rel)
        thumb = _ensure_thumb(local)
        return FileResponse(thumb, media_type="image/jpeg", headers=_THUMB_CACHE_HEADERS)
    except (FileNotFoundError, ValueError):
        pass
    try:
        vod_rec = catalog.get_vod(vid, day_key=day)
    except FileNotFoundError as exc:
        raise _error(404, str(exc)) from exc
    poster = catalog.poster_for(vod_rec, rel)
    if not poster:
        raise _error(404, "no local file for thumbnail")
    try:
        return FileResponse(catalog.resolve_local(vid, poster), headers=_THUMB_CACHE_HEADERS)
    except FileNotFoundError:
        return _gcs_media(request, vid, poster, day=day)


def _thumb_seek_seconds(src: Path) -> str:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(src),
        ],
        capture_output=True,
        text=True,
    )
    try:
        duration = float((proc.stdout or "").strip())
    except ValueError:
        duration = 2.0
    if duration <= 0.05:
        return "0"
    seek = min(2.0, max(0.0, duration * 0.35))
    return f"{seek:.3f}"


def _ensure_thumb(src: Path) -> Path:
    key = hashlib.sha1(f"{src}:{src.stat().st_mtime}:{src.stat().st_size}".encode()).hexdigest()[:20]
    dest = CACHE / "thumbs" / f"{key}.jpg"
    if dest.is_file() and dest.stat().st_size > 1000:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            _thumb_seek_seconds(src),
            "-i",
            str(src),
            "-frames:v",
            "1",
            "-vf",
            "scale=480:-2",
            "-pix_fmt",
            "yuvj420p",
            "-y",
            str(dest),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size < 1000:
        if dest.is_file():
            dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="ffmpeg thumbnail failed")
    return dest


def _gcs_media(
    request: Request,
    vod_id: str,
    rel: str,
    *,
    day: str | None = None,
) -> Response:
    if not catalog.gcs_ready():
        raise _error(404, rel)
    import storage_gcs as gcs

    object_name = catalog.gcs_object_name(vod_id, rel, day_key=day)
    if not object_name:
        raise _error(404, rel)
    info = gcs.blob_stat(object_name)
    if info is None:
        raise _error(404, rel)
    size = int(info["size"])
    mime = info.get("contentType") or mimetypes.guess_type(rel)[0] or "video/mp4"
    start, end, status, headers = _range_for(request.headers.get("range"), size)
    headers.update(
        {
            "Accept-Ranges": "bytes",
            "Content-Type": mime,
            "Content-Length": str(end - start + 1),
            "Cache-Control": "private, max-age=120",
        }
    )

    def chunks():
        pos = start
        chunk = 1024 * 1024
        while pos <= end:
            chunk_end = min(pos + chunk - 1, end)
            yield gcs.blob_bytes(object_name, start=pos, end=chunk_end)
            pos = chunk_end + 1

    return StreamingResponse(chunks(), status_code=status, headers=headers)


def _range_for(header: str | None, size: int) -> tuple[int, int, int, dict[str, str]]:
    if not header:
        return 0, size - 1, 200, {}
    match = RANGE_RE.fullmatch(header.strip())
    if not match:
        return 0, size - 1, 200, {}
    a, b = match.group(1), match.group(2)
    start = int(a) if a else 0
    end = int(b) if b else size - 1
    start = max(0, min(start, size - 1))
    end = max(start, min(end, size - 1))
    return start, end, 206, {"Content-Range": f"bytes {start}-{end}/{size}"}


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Local VOD archive viewer")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    print(f"viewer → http://{args.host}:{args.port}", flush=True)
    uvicorn.run("viewer.app:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
