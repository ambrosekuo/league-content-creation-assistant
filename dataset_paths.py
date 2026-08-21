"""Local data/ folder naming: {dayKey}_{vodId} (e.g. aug16_2026_2845534914)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DAY_KEY_RE = re.compile(r"^[a-z]{3}\d{2}_\d{4}$", re.I)
DATED_DIR_RE = re.compile(r"^([a-z]{3}\d{2}_\d{4})_(\d+)$", re.I)
LEGACY_DIR_RE = re.compile(r"^v?(\d+)$")

SKIP_DIR_PREFIXES = ("_", ".")


def game_index_from_only(token: str) -> int | None:
    """Parse g14 / gam14 style game selectors."""
    tok = token.strip().lower()
    if tok.startswith("gam") and tok[3:].isdigit():
        return int(tok[3:])
    if tok.startswith("g") and tok[1:].isdigit():
        return int(tok[1:])
    return None


def game_only_matches(name: str, only: str) -> bool:
    """Match clip folders (g14_…) or weave files (gam14_…)."""
    low = name.lower()
    tok = only.strip().lower()
    if not tok:
        return True
    if tok in low:
        return True
    idx = game_index_from_only(tok)
    if idx is None:
        return False
    prefixes = (f"g{idx:02d}_", f"g{idx}_", f"gam{idx:02d}_", f"gam{idx}_")
    return any(p in low for p in prefixes)


def vod_id_from_dir_name(name: str) -> str:
    """Extract Twitch VOD id from a data/ folder name."""
    cleaned = name.strip()
    match = DATED_DIR_RE.match(cleaned)
    if match:
        return match.group(2)
    match = LEGACY_DIR_RE.match(cleaned)
    if match:
        return match.group(1)
    return cleaned.lstrip("v")


def day_key_from_dir_name(name: str) -> str | None:
    match = DATED_DIR_RE.match(name.strip())
    return match.group(1).lower() if match else None


def dated_dir_name(day_key: str, vod_id: str) -> str:
    vid = vod_id.strip().lstrip("v")
    day = day_key.strip().lower()
    return f"{day}_{vid}"


def _meta_timestamp(dataset_dir: Path) -> int | None:
    for name in ("metadata.json", "source.info.json"):
        path = dataset_dir / name
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        ts = payload.get("timestamp")
        if ts is not None:
            try:
                return int(ts)
            except (TypeError, ValueError):
                continue
    return None


def day_key_for_dataset(dataset_dir: Path) -> str | None:
    """Derive GCS-style day key from metadata timestamp or folder mtime."""
    from storage_gcs import day_key_from_dt

    ts = _meta_timestamp(dataset_dir)
    if ts is not None:
        return day_key_from_dt(datetime.fromtimestamp(ts, tz=timezone.utc))
    try:
        mtime = dataset_dir.stat().st_mtime
        return day_key_from_dt(datetime.fromtimestamp(mtime, tz=timezone.utc))
    except OSError:
        return None


def find_dataset_dir(root: Path, vod_id: str) -> Path | None:
    """Find data/{day}_{id}/ or legacy data/{id}/."""
    vid = vod_id.strip().lstrip("v")
    if not root.is_dir():
        return None

    legacy = root / vid
    if legacy.is_dir():
        return legacy

    matches: list[Path] = []
    for path in root.iterdir():
        if not path.is_dir() or path.name.startswith(SKIP_DIR_PREFIXES):
            continue
        match = DATED_DIR_RE.match(path.name)
        if match and match.group(2) == vid:
            matches.append(path)
    if not matches:
        return None
    return sorted(matches, key=lambda p: p.name)[-1]


def resolve_dataset_dir(
    root: Path,
    vod_id: str,
    *,
    day_key: str | None = None,
) -> Path:
    """Return an existing folder or the dated path to create for a new ingest."""
    found = find_dataset_dir(root, vod_id)
    if found:
        return found
    vid = vod_id.strip().lstrip("v")
    if day_key:
        return root / dated_dir_name(day_key, vid)
    return root / vid


def iter_local_vod_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    out: list[Path] = []
    for path in sorted(root.iterdir()):
        if not path.is_dir() or path.name.startswith(SKIP_DIR_PREFIXES):
            continue
        if DATED_DIR_RE.match(path.name) or LEGACY_DIR_RE.match(path.name):
            out.append(path)
    return out


def rename_legacy_dirs(root: Path, *, dry_run: bool = False) -> list[dict[str, Any]]:
    """Rename numeric data/{id}/ folders to data/{dayKey}_{id}/."""
    results: list[dict[str, Any]] = []
    for path in iter_local_vod_dirs(root):
        if not LEGACY_DIR_RE.match(path.name):
            continue
        vid = vod_id_from_dir_name(path.name)
        day = day_key_for_dataset(path)
        if not day:
            results.append({"from": path.name, "status": "skip", "reason": "no day key"})
            continue
        target = root / dated_dir_name(day, vid)
        if target.exists():
            results.append({"from": path.name, "to": target.name, "status": "skip", "reason": "target exists"})
            continue
        if dry_run:
            results.append({"from": path.name, "to": target.name, "status": "dry_run"})
            continue
        path.rename(target)
        results.append({"from": path.name, "to": target.name, "status": "renamed"})
    return results


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Rename legacy data/{vodId}/ folders to dated names.")
    parser.add_argument("--root", type=Path, default=Path("data"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rows = rename_legacy_dirs(args.root.resolve(), dry_run=args.dry_run)
    for row in rows:
        print(row)
    renamed = sum(1 for row in rows if row.get("status") == "renamed")
    print(f"Done: {renamed} renamed, {len(rows)} total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
