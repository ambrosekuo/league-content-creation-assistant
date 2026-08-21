"""Shared types for clip classification."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from clip_classifiers.taxonomy import infer_hook_style, normalize_category, normalize_hook_style, pick_hook


@dataclass
class ClipSignals:
    event_type: list[str]
    kills_in_10s: int
    deaths_in_10s: int
    assists_in_10s: int
    low_hp: bool
    game_end_nearby: bool
    reaction_score: float
    reaction_level: str
    champion: str
    opponent: str | None
    lane_opponent: str | None
    vs_lane: bool
    died: bool
    win: bool | None
    duration: float | None
    voice_hits: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ClassificationResult:
    primary: str
    secondary: list[str]
    confidence: float
    reason: str
    hook_style: str
    signals: dict[str, Any]
    classifier: str
    ambiguous: bool = False
    candidates: list[dict[str, Any]] | None = None

    def to_record(self) -> dict[str, Any]:
        primary = normalize_category(self.primary)
        secondary = [normalize_category(s) for s in self.secondary if normalize_category(s) != primary][:3]
        hook_style = normalize_hook_style(self.hook_style, primary=primary)
        return {
            "interpretation": {
                "primary": primary,
                "secondary": secondary,
                "category": primary,
                "confidence": round(float(self.confidence), 3),
                "reason": self.reason,
                "hook_style": hook_style,
                "signals": self.signals,
                "classifier": self.classifier,
                "ambiguous": self.ambiguous,
                "candidates": self.candidates,
            },
            "hook": {
                "text": pick_hook(hook_style, primary=primary),
                "source": f"template_{hook_style}",
            },
        }
