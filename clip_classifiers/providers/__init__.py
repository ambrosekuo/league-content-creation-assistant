"""AI provider factory."""

from __future__ import annotations

import os

from env_loader import load_dotenv

from clip_classifiers.providers.base import AIProvider
from clip_classifiers.providers.gemini_provider import GeminiProvider
from clip_classifiers.providers.openai_provider import OpenAIProvider

_PROVIDERS: dict[str, type] = {
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
}


def get_ai_provider(name: str | None = None) -> AIProvider:
    load_dotenv()
    key = (name or os.environ.get("CLIP_AI_PROVIDER") or "openai").strip().lower()
    factory = _PROVIDERS.get(key)
    if factory is None:
        known = ", ".join(sorted(_PROVIDERS))
        raise ValueError(f"unknown CLIP_AI_PROVIDER {key!r} (known: {known})")
    return factory()
