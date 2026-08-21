"""Deterministic rule-based clip classifier."""

from __future__ import annotations

from typing import Any

from clip_classifiers.taxonomy import infer_hook_style, normalize_category
from clip_classifiers.types import ClassificationResult, ClipSignals

RULE_CONFIDENCE_THRESHOLD = 0.80
OBJECTIVE_TYPES = frozenset(
    {"baron", "dragon", "elder", "herald", "horde", "inhibitor", "tower", "nexus"}
)


class RuleClassifier:
    name = "rules_v1"

    def classify(
        self,
        clip: dict[str, Any],
        signals: ClipSignals,
        *,
        dataset_dir: Any = None,
        frames: list[Any] | None = None,
    ) -> ClassificationResult:
        del clip, dataset_dir, frames
        candidates = self._score_candidates(signals)
        candidates.sort(key=lambda row: row["confidence"], reverse=True)
        top = candidates[0]
        second_conf = candidates[1]["confidence"] if len(candidates) > 1 else 0.0
        gap = top["confidence"] - second_conf
        ambiguous = top["confidence"] < RULE_CONFIDENCE_THRESHOLD or gap < 0.12
        secondary = [row["primary"] for row in candidates[1:4] if row["primary"] != top["primary"]]
        reason = top["reason"]
        if ambiguous and len(candidates) > 1:
            alts = ", ".join(f"{row['primary']} {row['confidence']:.2f}" for row in candidates[:3])
            reason = f"{reason} Alternatives: {alts}."
        return ClassificationResult(
            primary=top["primary"],
            secondary=secondary[:3],
            confidence=top["confidence"],
            reason=reason,
            hook_style=infer_hook_style(top["primary"], secondary=secondary),
            signals=signals.to_dict(),
            classifier=self.name,
            ambiguous=ambiguous,
            candidates=candidates[:5],
        )

    def _score_candidates(self, signals: ClipSignals) -> list[dict[str, Any]]:
        scores: dict[str, tuple[float, str]] = {}

        if signals.kills_in_10s >= 2 and signals.deaths_in_10s == 0:
            conf = min(0.97, 0.84 + 0.05 * signals.kills_in_10s)
            scores["multikill"] = (conf, f"{signals.kills_in_10s} kills in 10s with no death.")

        if signals.kills_in_10s >= 1 and signals.deaths_in_10s >= 1:
            scores["trade"] = (0.95, "Kill and death within 10 seconds.")

        if signals.game_end_nearby:
            scores["game_end"] = (0.93, "Game end event is nearby.")

        if signals.died and signals.kills_in_10s == 0:
            scores["mistake"] = (0.9, "Death without a kill in the clip window.")

        if any(t in OBJECTIVE_TYPES for t in signals.event_type):
            scores["decision"] = (0.86, "Objective or structure event in clip metadata.")

        if signals.kills_in_10s >= 1 and not signals.died and signals.vs_lane and signals.reaction_score >= 0.5:
            scores["outplay"] = (
                min(0.9, 0.68 + signals.reaction_score * 0.22),
                "Lane opponent outplay with strong reaction.",
            )

        if signals.low_hp and signals.kills_in_10s >= 1 and not signals.died:
            scores["survival"] = (0.82, "Low-health survival with a kill.")

        if signals.reaction_score >= 0.55 and not signals.died:
            scores["reaction"] = (
                min(0.85, 0.55 + signals.reaction_score * 0.3),
                f"Strong streamer reaction ({signals.reaction_level}).",
            )

        if signals.kills_in_10s >= 1 and not signals.died and signals.reaction_score >= 0.35:
            scores["survival"] = scores.get(
                "survival",
                (0.61, "Single kill with no death; possible escape context."),
            )
            scores["outplay"] = scores.get(
                "outplay",
                (0.57, "Single kill; possible outplay depending on context."),
            )

        if signals.kills_in_10s >= 1 and not signals.died:
            scores["chase"] = (0.52, "Kill present; may involve a chase.")

        scores.setdefault(
            "ordinary",
            (0.45, "No strong deterministic pattern matched."),
        )

        return [
            {
                "primary": normalize_category(name),
                "confidence": round(conf, 3),
                "reason": reason,
            }
            for name, (conf, reason) in scores.items()
        ]
