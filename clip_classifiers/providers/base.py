"""Generic AI provider interface for clip classification."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from clip_classifiers.types import ClipSignals


@runtime_checkable
class AIProvider(Protocol):
    name: str

    def classify_metadata(self, clip: dict[str, Any], signals: ClipSignals) -> dict[str, Any]:
        """Return parsed classification JSON from metadata-only input."""
        ...


def build_ai_payload(clip: dict[str, Any], signals: ClipSignals) -> dict[str, Any]:
    """Compact input for level-1 (metadata + audio metrics). No video."""
    return {
        "clip": {
            "id": clip.get("id"),
            "game_id": clip.get("gameId"),
            "champion": signals.champion,
            "opponent": signals.opponent,
            "lane_opponent": signals.lane_opponent,
            "event_type": signals.event_type,
            "game_time": clip.get("gameTime"),
            "win": signals.win,
            "duration": signals.duration,
            "vs_lane": signals.vs_lane,
            "died": signals.died,
        },
        "signals": {
            "kills_in_10s": signals.kills_in_10s,
            "deaths_in_10s": signals.deaths_in_10s,
            "assists_in_10s": signals.assists_in_10s,
            "low_hp": signals.low_hp,
            "game_end_nearby": signals.game_end_nearby,
            "reaction_score": signals.reaction_score,
            "reaction_level": signals.reaction_level,
            "voice_hits": signals.voice_hits,
        },
    }
