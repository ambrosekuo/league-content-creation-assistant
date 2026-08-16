#!/usr/bin/env python3
"""Local bucket viewer: browse processed videos, requeue jobs, keep/skip."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import subprocess
import sys
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

from env_loader import load_dotenv

from viewer import catalog, jobs, store

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


def _error(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


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
    review = store.get_vod(vod["vodId"])
    for video in vod.get("videos") or []:
        video["poster"] = catalog.poster_for(vod, video["path"])
        video["review"] = (review.get("files") or {}).get(video["path"])
    vod["notes"] = review.get("notes") or ""
    vod["reviews"] = review.get("files") or {}
    vod["jobs"] = jobs.catalog()
    return vod


@app.get("/api/jobs")
def api_jobs() -> dict[str, Any]:
    return {"jobs": jobs.catalog()}


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
        vod_rec = catalog.get_vod(vid, day_key=day)
    except FileNotFoundError as exc:
        raise _error(404, str(exc)) from exc
    poster = catalog.poster_for(vod_rec, rel)
    if poster:
        try:
            return FileResponse(catalog.resolve_local(vid, poster))
        except FileNotFoundError:
            return _gcs_media(request, vid, poster, day=day)
    try:
        local = catalog.resolve_local(vid, rel)
    except (FileNotFoundError, ValueError):
        if poster:
            raise _error(404, "no thumbnail")
        raise _error(404, "no local file for thumbnail")
    thumb = _ensure_thumb(local)
    return FileResponse(thumb, media_type="image/jpeg")


def _ensure_thumb(src: Path) -> Path:
    key = hashlib.sha1(f"{src}:{src.stat().st_mtime}:{src.stat().st_size}".encode()).hexdigest()[:20]
    dest = CACHE / "thumbs" / f"{key}.jpg"
    if dest.is_file():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "2",
            "-i",
            str(src),
            "-frames:v",
            "1",
            "-vf",
            "scale=480:-2",
            "-y",
            str(dest),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not dest.is_file():
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
