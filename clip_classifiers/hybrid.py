"""Rules-first hybrid classifier: AI only for ambiguous clips."""

from __future__ import annotations

from typing import Any

from clip_classifiers.ai import AIClassifier
from clip_classifiers.rules import RULE_CONFIDENCE_THRESHOLD, RuleClassifier
from clip_classifiers.types import ClassificationResult, ClipSignals


class HybridClassifier:
    name = "hybrid_v1"

    def __init__(self, *, ai: AIClassifier | None = None) -> None:
        self.rules = RuleClassifier()
        self.ai = ai or AIClassifier()

    def classify(
        self,
        clip: dict[str, Any],
        signals: ClipSignals,
        *,
        dataset_dir: Any = None,
        frames: list[Any] | None = None,
    ) -> ClassificationResult:
        ruled = self.rules.classify(clip, signals, dataset_dir=dataset_dir, frames=frames)
        if not ruled.ambiguous and ruled.confidence >= RULE_CONFIDENCE_THRESHOLD:
            ruled.reason = f"Rules confident ({ruled.confidence:.2f}): {ruled.reason}"
            ruled.classifier = self.name
            return ruled
        try:
            ai_result = self.ai.classify(clip, signals, dataset_dir=dataset_dir, frames=frames)
        except RuntimeError as exc:
            ruled.reason = f"Rules ambiguous; AI unavailable ({exc}). Using rules."
            ruled.classifier = self.name
            return ruled
        ai_result.classifier = self.name
        ai_result.reason = (
            f"Rules ambiguous ({ruled.primary} {ruled.confidence:.2f}); "
            f"AI chose {ai_result.primary} ({ai_result.confidence:.2f}). {ai_result.reason}"
        )
        ai_result.candidates = ruled.candidates
        return ai_result
