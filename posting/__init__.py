"""Upload rendered shorts to YouTube (private) and TikTok (draft inbox)."""

from __future__ import annotations

from posting.meta import (
    SCHEMA_VERSION,
    build_record,
    default_title,
    discover_shorts,
    read_sidecar,
    sidecar_path,
    weave_stem_for,
    write_sidecar,
)

__all__ = [
    "SCHEMA_VERSION",
    "build_record",
    "default_title",
    "discover_shorts",
    "read_sidecar",
    "sidecar_path",
    "weave_stem_for",
    "write_sidecar",
]
