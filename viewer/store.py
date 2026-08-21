"""Local review notes / keep-skip flags / clip ratings (never uploaded)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
STORE_PATH = ROOT / "data" / "_viewer" / "reviews.json"
CLIP_RATINGS = ("reject", "keep", "excellent", "godly", "manual_edit")
CLASSIFICATION_STATUSES = ("pending", "approved", "edited")
CLASSIFICATION_SOURCES = ("rules", "ai")
TITLE_STATUSES = ("pending", "approved", "edited")
QUEUE_FILES = {
    "godly": "godly.json",
    "excellent": "excellent.json",
    "keep": "keep.json",
    "manual_edit": "manual_edit.json",
    "reject": "rejected.json",
}


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


def clip_review_dir(vod_id: str, day_key: str | None = None) -> Path:
    vid = vod_id.strip().lstrip("v")
    day = (day_key or "").strip().lower()
    name = f"{day}_{vid}" if day and day != "local" else vid
    return ROOT / "data" / "_viewer" / name


def get_clip_selections(vod_id: str, day_key: str | None = None) -> dict[str, Any]:
    path = clip_review_dir(vod_id, day_key) / "selections.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def set_clip_selection(
    vod_id: str,
    day_key: str | None,
    clip_id: str,
    rating: str | None,
) -> dict[str, Any]:
    data = get_clip_selections(vod_id, day_key)
    key = clip_id.strip()
    if not key:
        raise ValueError("clip id required")
    if not rating:
        data.pop(key, None)
    else:
        if rating not in CLIP_RATINGS:
            raise ValueError(f"rating must be {', '.join(CLIP_RATINGS)}, or empty")
        data[key] = {
            "rating": rating,
            "reviewed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    dest = clip_review_dir(vod_id, day_key) / "selections.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return data


def _classifications_path(vod_id: str, day_key: str | None) -> Path:
    return clip_review_dir(vod_id, day_key) / "classifications.json"


def normalize_classification_entry(rec: dict[str, Any] | None) -> dict[str, Any]:
    if not rec:
        return {"rules": None, "ai": None}
    if "interpretation" in rec:
        source = str(rec.get("source") or "rules")
        if source not in CLASSIFICATION_SOURCES:
            source = "rules"
        return {
            "rules": rec if source == "rules" else None,
            "ai": rec if source == "ai" else None,
        }
    return {
        "rules": rec.get("rules") if isinstance(rec.get("rules"), dict) else None,
        "ai": rec.get("ai") if isinstance(rec.get("ai"), dict) else None,
    }


def clip_is_classified(entry: dict[str, Any] | None) -> bool:
    normalized = normalize_classification_entry(entry if isinstance(entry, dict) else None)
    return bool(normalized.get("rules") or normalized.get("ai"))


def get_classifications(vod_id: str, day_key: str | None = None) -> dict[str, Any]:
    path = _classifications_path(vod_id, day_key)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def set_classification(
    vod_id: str,
    day_key: str | None,
    clip_id: str,
    record: dict[str, Any],
    *,
    source: str = "rules",
) -> dict[str, Any]:
    if source not in CLASSIFICATION_SOURCES:
        raise ValueError(f"source must be {', '.join(CLASSIFICATION_SOURCES)}")
    data = get_classifications(vod_id, day_key)
    key = clip_id.strip()
    if not key:
        raise ValueError("clip id required")
    bundle = normalize_classification_entry(data.get(key))
    record = dict(record)
    record["source"] = source
    bundle[source] = record
    data[key] = bundle
    dest = _classifications_path(vod_id, day_key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return bundle


def update_classification_review(
    vod_id: str,
    day_key: str | None,
    clip_id: str,
    *,
    status: str,
    hook_text: str | None = None,
    source: str = "rules",
) -> dict[str, Any]:
    if source not in CLASSIFICATION_SOURCES:
        raise ValueError(f"source must be {', '.join(CLASSIFICATION_SOURCES)}")
    if status not in CLASSIFICATION_STATUSES:
        raise ValueError(f"status must be {', '.join(CLASSIFICATION_STATUSES)}")
    if status == "edited" and not (hook_text or "").strip():
        raise ValueError("hook_text required when status is edited")
    data = get_classifications(vod_id, day_key)
    key = clip_id.strip()
    bundle = normalize_classification_entry(data.get(key))
    rec = bundle.get(source)
    if not isinstance(rec, dict):
        raise ValueError(f"clip {key} has no {source} classification")
    rec = dict(rec)
    hook = dict(rec.get("hook") or {})
    if status == "edited":
        hook["text"] = hook_text.strip()
        hook["source"] = "user"
    rec["hook"] = hook
    rec["status"] = status
    rec["reviewed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    bundle[source] = rec
    data[key] = bundle
    dest = _classifications_path(vod_id, day_key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return bundle


def _titles_path(vod_id: str, day_key: str | None) -> Path:
    return clip_review_dir(vod_id, day_key) / "titles.json"


def get_titles(vod_id: str, day_key: str | None = None) -> dict[str, Any]:
    path = _titles_path(vod_id, day_key)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def title_is_generated(rec: dict[str, Any] | None) -> bool:
    if not isinstance(rec, dict):
        return False
    if (rec.get("selected") or "").strip():
        return True
    return bool(rec.get("suggestions"))


def set_title_record(
    vod_id: str,
    day_key: str | None,
    weave_stem: str,
    record: dict[str, Any],
) -> dict[str, Any]:
    data = get_titles(vod_id, day_key)
    key = weave_stem.strip()
    if not key:
        raise ValueError("weave stem required")
    record = dict(record)
    record.setdefault("status", "pending")
    record.setdefault("source", "ai")
    record["generated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    data[key] = record
    dest = _titles_path(vod_id, day_key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return record


def update_title_review(
    vod_id: str,
    day_key: str | None,
    weave_stem: str,
    *,
    status: str,
    selected: str | None = None,
    selected_style: str | None = None,
) -> dict[str, Any]:
    if status not in TITLE_STATUSES:
        raise ValueError(f"status must be {', '.join(TITLE_STATUSES)}")
    if status == "edited" and not (selected or "").strip():
        raise ValueError("selected required when status is edited")
    data = get_titles(vod_id, day_key)
    key = weave_stem.strip()
    rec = data.get(key)
    if not isinstance(rec, dict):
        raise ValueError(f"no title record for {key}")
    rec = dict(rec)
    if status == "edited":
        rec["selected"] = selected.strip()
    elif status == "approved":
        picked = (selected or rec.get("selected") or "").strip()
        if not picked and rec.get("suggestions"):
            picked = str(rec["suggestions"][0]).strip()
        if picked:
            rec["selected"] = picked
    style = (selected_style or "").strip()
    if style:
        rec["selectedStyle"] = style
    rec["status"] = status
    rec["reviewed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    data[key] = rec
    dest = _titles_path(vod_id, day_key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return rec


def write_approved_queues(
    vod_id: str,
    day_key: str | None,
    clips: list[dict[str, Any]],
    selections: dict[str, Any],
    row_fn,
) -> None:
    buckets: dict[str, list[dict[str, Any]]] = {rating: [] for rating in QUEUE_FILES}
    by_id = {clip["id"]: clip for clip in clips}
    for clip_id, rec in selections.items():
        rating = (rec or {}).get("rating")
        clip = by_id.get(clip_id)
        if rating not in buckets or clip is None:
            continue
        buckets[rating].append(row_fn(clip, rating=rating, reviewed_at=(rec or {}).get("reviewed_at")))
    approved = clip_review_dir(vod_id, day_key) / "approved"
    approved.mkdir(parents=True, exist_ok=True)
    for rating, filename in QUEUE_FILES.items():
        path = approved / filename
        path.write_text(
            json.dumps(buckets[rating], indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
