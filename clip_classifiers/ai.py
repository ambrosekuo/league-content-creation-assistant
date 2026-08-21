"""Metadata-only AI clip classifier (level 1)."""

from __future__ import annotations

from typing import Any

from clip_classifiers.providers import get_ai_provider
from clip_classifiers.providers.base import AIProvider
from clip_classifiers.taxonomy import infer_hook_style, normalize_category, normalize_hook_style
from clip_classifiers.types import ClassificationResult, ClipSignals


class AIClassifier:
    name = "ai_metadata_v1"

    def __init__(self, *, provider: AIProvider | None = None) -> None:
        self.provider = provider or get_ai_provider()

    def classify(
        self,
        clip: dict[str, Any],
        signals: ClipSignals,
        *,
        dataset_dir: Any = None,
        frames: list[Any] | None = None,
    ) -> ClassificationResult:
        if frames:
            raise RuntimeError("frame input is not supported yet (level 2)")
        del dataset_dir
        parsed = self.provider.classify_metadata(clip, signals)
        primary = normalize_category(parsed.get("primary"))
        secondary = [
            normalize_category(item)
            for item in (parsed.get("secondary") or [])
            if normalize_category(item) != primary
        ][:3]
        hook_style = normalize_hook_style(
            parsed.get("hook_style") or parsed.get("hook_family"),
            primary=primary,
        )
        if not parsed.get("hook_style") and not parsed.get("hook_family"):
            hook_style = infer_hook_style(primary, secondary=secondary)
        try:
            confidence = float(parsed.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = round(min(1.0, max(0.0, confidence)), 3)
        reason = str(parsed.get("reason") or "AI classification from metadata signals.").strip()
        classifier = f"{self.name}:{getattr(self.provider, 'name', 'unknown')}"
        model = getattr(self.provider, "model", None)
        if model:
            classifier = f"{classifier}:{model}"
        return ClassificationResult(
            primary=primary,
            secondary=secondary,
            confidence=confidence,
            reason=reason,
            hook_style=hook_style,
            signals=signals.to_dict(),
            classifier=classifier,
            ambiguous=confidence < 0.8,
            candidates=None,
        )
