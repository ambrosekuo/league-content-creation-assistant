"""Pluggable clip classifiers: rules, AI, hybrid."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from clip_classifiers.ai import AIClassifier
from clip_classifiers.hybrid import HybridClassifier
from clip_classifiers.rules import RuleClassifier
from clip_classifiers.signals import extract_signals
from clip_classifiers.types import ClassificationResult

ClassifierMode = Literal["rules", "ai", "hybrid"]

_CLASSIFIERS = {
    "rules": RuleClassifier,
    "ai": AIClassifier,
    "hybrid": HybridClassifier,
}


def get_classifier(mode: ClassifierMode):
    factory = _CLASSIFIERS.get(mode)
    if factory is None:
        raise ValueError(f"unknown classifier mode: {mode}")
    return factory()


def classify_clip(
    clip: dict[str, Any],
    *,
    dataset_dir: Path | None,
    mode: ClassifierMode = "rules",
) -> dict[str, Any]:
    """Classify one review clip. Returns { interpretation, hook }."""
    signals = extract_signals(clip, dataset_dir=dataset_dir)
    classifier = get_classifier(mode)
    result = classifier.classify(clip, signals, dataset_dir=dataset_dir)
    return result.to_record()


def wrap_record(result: dict[str, Any], *, source: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        **result,
        "source": source,
        "status": "pending",
        "classified_at": now,
        "reviewed_at": None,
    }
