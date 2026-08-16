"""Local review notes / keep-skip flags (never uploaded)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
STORE_PATH = ROOT / "data" / "_viewer" / "reviews.json"


def _load() -> dict[str, Any]:
    if not STORE_PATH.is_file():
        return {"vods": {}}
    try:
        return json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"vods": {}}


def _save(payload: dict[str, Any]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def get_vod(vod_id: str) -> dict[str, Any]:
    data = _load()
    rec = (data.get("vods") or {}).get(vod_id) or {}
    return {
        "notes": str(rec.get("notes") or ""),
        "files": dict(rec.get("files") or {}),
    }


def set_notes(vod_id: str, notes: str) -> dict[str, Any]:
    data = _load()
    vods = data.setdefault("vods", {})
    rec = vods.setdefault(vod_id, {"notes": "", "files": {}})
    rec["notes"] = notes
    _save(data)
    return get_vod(vod_id)


def set_file_review(vod_id: str, rel_path: str, status: str | None) -> dict[str, Any]:
    data = _load()
    vods = data.setdefault("vods", {})
    rec = vods.setdefault(vod_id, {"notes": "", "files": {}})
    files = rec.setdefault("files", {})
    if not status:
        files.pop(rel_path, None)
    else:
        if status not in {"keep", "skip"}:
            raise ValueError("status must be keep, skip, or empty")
        files[rel_path] = status
    _save(data)
    return get_vod(vod_id)
