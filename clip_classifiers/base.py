"""Classifier interface."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from clip_classifiers.types import ClassificationResult, ClipSignals


@runtime_checkable
class ClipClassifier(Protocol):
    name: str

    def classify(
        self,
        clip: dict[str, Any],
        signals: ClipSignals,
        *,
        dataset_dir: Any = None,
        frames: list[Any] | None = None,
    ) -> ClassificationResult:
        ...
