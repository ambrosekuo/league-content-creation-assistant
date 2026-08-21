"""Fixed taxonomy, hook styles, and engagement copy."""

from __future__ import annotations

PRIMARY_CATEGORIES = (
    "survival",
    "outplay",
    "multikill",
    "mistake",
    "decision",
    "chase",
    "trade",
    "game_end",
    "reaction",
    "ordinary",
)

HOOK_STYLES = (
    "prediction",
    "judgment",
    "decision",
    "mistake",
    "rating",
    "rank_guess",
    "worth",
)

# What happened → default engagement angle (rules classifier; AI overrides).
DEFAULT_HOOK_STYLE: dict[str, str] = {
    "survival": "prediction",
    "outplay": "judgment",
    "multikill": "prediction",
    "mistake": "mistake",
    "decision": "decision",
    "chase": "decision",
    "trade": "worth",
    "game_end": "prediction",
    "reaction": "judgment",
    "ordinary": "decision",
}

HOOKS_BY_STYLE: dict[str, list[str]] = {
    "prediction": [
        "Do I live here?",
        "How does this end?",
        "Do I get him?",
        "How many do I get?",
        "Can we end here?",
    ],
    "judgment": [
        "Clean or lucky?",
        "Outplay or int?",
        "Was this actually good?",
        "Did he get outplayed?",
        "Was that reaction justified?",
    ],
    "decision": [
        "Would you go in here?",
        "Do you keep chasing?",
        "What would you do?",
        "Do you commit?",
        "Would you take this fight?",
    ],
    "mistake": [
        "Spot the mistake.",
        "Where did this go wrong?",
        "What did I do wrong?",
        "What would you do differently?",
    ],
    "rating": [
        "Rate this 1-10.",
        "How clean was this?",
    ],
    "rank_guess": [
        "Guess the elo.",
        "What rank does this look like?",
    ],
    "worth": [
        "Worth?",
        "Good trade or int?",
        "Was this worth it?",
        "Would you take this trade?",
    ],
}

AI_SYSTEM_PROMPT = """You classify League of Legends highlight clips for short-form content.

First determine what happened in the clip.

Primary category (choose exactly one):
survival, outplay, multikill, mistake, decision, chase, trade, game_end, reaction, ordinary

You may select up to 3 secondary categories from the same list (excluding primary).

Then choose the best engagement angle for a hook question:

prediction — viewer can predict what happens next
judgment — viewer can debate whether the play was good, lucky, clean, or bad
decision — viewer can choose between gameplay options
mistake — viewer can identify what went wrong
rating — the play suits a 1-10 rating prompt
rank_guess — gameplay could prompt an elo/rank guess
worth — viewers can debate whether a tradeoff was worth it

Choose engagement angles based only on supplied evidence. Do not invent events.
Prefer questions understandable BEFORE the payoff happens.
Do not write narration or captions (no "Watch this", "GG", "Secured").

Return JSON only:
{
  "primary": "outplay",
  "secondary": ["survival"],
  "confidence": 0.87,
  "reason": "Short explanation citing supplied evidence.",
  "hook_style": "prediction"
}

hook_style is independent of primary — pick the angle that best creates curiosity, judgment, or disagreement."""


def normalize_category(value: str | None) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "multi_kill": "multikill",
        "multi": "multikill",
        "death": "mistake",
        "objective": "decision",
        "other": "ordinary",
    }
    text = aliases.get(text, text)
    return text if text in PRIMARY_CATEGORIES else "ordinary"


def normalize_hook_style(value: str | None, *, primary: str | None = None) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "predict": "prediction",
        "judge": "judgment",
        "rate": "rating",
        "rank": "rank_guess",
        "hook_family": "",  # legacy; fall through
    }
    text = aliases.get(text, text)
    if text in HOOK_STYLES:
        return text
    # Legacy: hook_family matched a primary category
    if text in PRIMARY_CATEGORIES:
        return DEFAULT_HOOK_STYLE.get(text, "decision")
    if primary:
        return DEFAULT_HOOK_STYLE.get(normalize_category(primary), "decision")
    return "decision"


def infer_hook_style(primary: str, *, secondary: list[str] | None = None) -> str:
    """Rules path: map what happened → default engagement angle."""
    del secondary
    return DEFAULT_HOOK_STYLE.get(normalize_category(primary), "decision")


# Override when (hook_style, primary) needs a specific question.
STYLE_PRIMARY_HOOK: dict[tuple[str, str], str] = {
    ("prediction", "survival"): "Do I live here?",
    ("prediction", "multikill"): "How many do I get?",
    ("prediction", "game_end"): "Can we end here?",
    ("prediction", "outplay"): "Do I get him?",
    ("prediction", "chase"): "Do you keep going?",
    ("judgment", "outplay"): "Clean or lucky?",
    ("judgment", "reaction"): "Was that reaction justified?",
    ("decision", "decision"): "Would you go in here?",
    ("decision", "chase"): "Would you chase this?",
    ("decision", "ordinary"): "What would you do here?",
    ("mistake", "mistake"): "Spot the mistake.",
    ("worth", "trade"): "Worth?",
    ("rating", "outplay"): "Rate this 1-10.",
}


def pick_hook(hook_style: str, *, primary: str | None = None) -> str:
    style = normalize_hook_style(hook_style, primary=primary)
    cat = normalize_category(primary) if primary else ""
    keyed = STYLE_PRIMARY_HOOK.get((style, cat))
    if keyed:
        return keyed
    for text in HOOKS_BY_STYLE.get(style) or HOOKS_BY_STYLE["decision"]:
        if text.strip():
            return text
    return ""
