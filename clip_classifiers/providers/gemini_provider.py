"""Gemini provider placeholder for future vision/video classification."""

from __future__ import annotations

from typing import Any

from clip_classifiers.types import ClipSignals


class GeminiProvider:
    name = "gemini"

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        del api_key, model

    def classify_metadata(self, clip: dict[str, Any], signals: ClipSignals) -> dict[str, Any]:
        del clip, signals
        raise RuntimeError(
            "Gemini provider is not implemented yet. "
            "Set CLIP_AI_PROVIDER=openai or implement clip_classifiers/providers/gemini_provider.py."
        )
