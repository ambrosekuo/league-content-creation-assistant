"""Load KEY=VALUE pairs from a .env file into os.environ (no overwrite)."""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: Path | None = None) -> Path | None:
    """Load the first existing .env from common project locations."""
    candidates: list[Path] = []
    if path is not None:
        candidates.append(path)
    else:
        here = Path.cwd()
        candidates.extend(
            [
                here / ".env",
                here.parent / ".env",
                Path(__file__).resolve().parent / ".env",
            ]
        )

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if not resolved.is_file():
            continue
        _apply_env_file(resolved)
        return resolved
    return None


def _apply_env_file(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        # Do not override variables already set in the shell.
        os.environ.setdefault(key, value)
