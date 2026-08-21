#!/usr/bin/env python3
"""Clip classification facade for the review UI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clip_classifiers import ClassifierMode, classify_clip as _classify

__all__ = ["classify_clip", "extract_signals"]

extract_signals = __import__("clip_classifiers.signals", fromlist=["extract_signals"]).extract_signals


def classify_clip(
    clip: dict[str, Any],
    *,
    dataset_dir: Path | None,
    mode: ClassifierMode = "rules",
) -> dict[str, Any]:
    return _classify(clip, dataset_dir=dataset_dir, mode=mode)
