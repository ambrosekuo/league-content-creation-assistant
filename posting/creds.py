"""Where upload credentials and cached tokens live.

Both secrets/ and .secrets/ are gitignored. Drop the Google OAuth client json in
either one and the uploader finds it; no path needs to go in .env.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SECRET_DIRS = ("secrets", ".secrets")


def secret_dir() -> Path:
    """Existing secrets folder, else the dotted one (created on first write)."""
    for name in SECRET_DIRS:
        path = ROOT / name
        if path.is_dir():
            return path
    return ROOT / ".secrets"


def find_client_secrets(pattern: str = "*client_secret*.json") -> Path | None:
    for name in SECRET_DIRS:
        folder = ROOT / name
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob(pattern)):
            if path.is_file():
                return path
    return None


def find_secret_file(filename: str) -> Path | None:
    for name in SECRET_DIRS:
        path = ROOT / name / filename
        if path.is_file():
            return path
    return None


def read_json_secret(filename: str) -> dict[str, Any]:
    """A small {"client_key": ..., "client_secret": ...} style file, or {} if absent."""
    path = find_secret_file(filename)
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    # Tolerate the whole thing being wrapped, the way Google nests under "installed".
    for key in ("installed", "tiktok", "app"):
        inner = payload.get(key)
        if isinstance(inner, dict):
            merged = {**inner, **{k: v for k, v in payload.items() if k != key}}
            return merged
    return payload
