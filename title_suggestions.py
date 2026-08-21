"""Generate short-form upload hooks for decorated portrait exports via OpenAI."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TIMEOUT = 90.0
DEFAULT_RETRIES = 2
HOOK_COUNT = 5

HOOK_STYLE_ENUM = [
    "curiosity",
    "mistake",
    "educational",
    "matchup",
    "challenge",
    "disbelief",
    "cocky",
    "self_deprecating",
    "outcome_tease",
    "observation",
    "debate",
    "ultra_short",
    "reaction",
]

TITLE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hooks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "style": {"type": "string", "enum": HOOK_STYLE_ENUM},
                    "text": {"type": "string"},
                },
                "required": ["style", "text"],
                "additionalProperties": False,
            },
            "minItems": HOOK_COUNT,
            "maxItems": HOOK_COUNT,
        },
        "best_hook": {"type": "string"},
        "best_reason": {"type": "string"},
        "hashtags": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 6,
        },
    },
    "required": ["hooks", "best_hook", "best_reason", "hashtags"],
    "additionalProperties": False,
}

TITLE_SYSTEM_PROMPT = f"""You write scroll-stopping hooks for League of Legends game-compilation Shorts (TikTok / Reels / Shorts).

NOT a summary. NOT SEO. NOT "[Champion] vs [Champion]: …".
Sell the overall game story — matchup vibe, experiment, mental, diff, domination, inting, ff15 energy.

Rules:
- Exactly {HOOK_COUNT} hooks in the hooks array — each a different style from the enum.
- 3-8 words each (ultra_short: 2-4).
- Casual modern creator voice. Lowercase ok. 💀 ok sparingly.
- Do not repeat the same word/vibe across hooks.
- Do not spoil outcome unless teasing (outcome_tease).
- Skip filler: tough matchup, epic outplay, brutal loss, intense battle.
- _internal is reasoning-only metadata.

Pick best_hook from your five. best_reason = one sentence on why it creates curiosity.
hashtags: 0-6 tags, no # prefix."""

TITLE_USER_NOTES_PROMPT = f"""CRITICAL — creatorAngle / userNotes is the MAIN story. This overrides matchup, events, and generic drama.

When creatorAngle or userNotes is present:
- ALL {HOOK_COUNT} hooks must riff on that angle — not generic "will X survive?" or "learn this combo" unless it ties directly to the angle.
- At least 3 hooks should reuse key words or concepts from creatorAngle (rank, role, experiment, mental, diff, etc.).
- Matchup/champion names are optional garnish — use only if they support the creator angle.
- Ask yourself: "would the creator recognize this as THEIR story?" If not, rewrite.
- Generic hooks that ignore creatorAngle are wrong even if they sound catchy.

Example: creatorAngle "Carrying Challenger players" →
  good: "carrying challs on leblanc", "chall lobby diff", "they queued me into chall"
  bad: "will ambessa survive?", "learn this combo", "how did they let this happen?"
"""


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_caption_lines(portrait_dir: Path, weave_stem: str, *, limit: int = 8) -> list[str]:
    for name in (
        f"{weave_stem}_portrait_captions.json",
        f"{weave_stem}_portrait_decorated_captions.json",
    ):
        path = portrait_dir / name
        if not path.is_file():
            continue
        payload = _load_json(path)
        if not payload:
            continue
        lines: list[str] = []
        for cue in payload.get("cues") or []:
            text = str(cue.get("text") or "").strip()
            if text and text not in lines:
                lines.append(text)
            if len(lines) >= limit:
                return lines
    return []


def _clip_light(
    clip: dict[str, Any],
    *,
    classifications: dict[str, Any],
    selections: dict[str, Any],
) -> dict[str, Any]:
    types = [str(t).lower() for t in (clip.get("types") or []) if str(t).strip()]
    rating = clip.get("rating") or (selections.get(clip["id"]) or {}).get("rating")
    bundle = classifications.get(clip["id"]) or {}
    rec = bundle.get("ai") or bundle.get("rules")
    interp = (rec or {}).get("interpretation") or {} if isinstance(rec, dict) else {}
    row: dict[str, Any] = {
        "events": types,
        "gameTime": clip.get("gameTime"),
        "rating": rating,
    }
    if interp.get("primary"):
        row["theme"] = interp.get("primary")
    if interp.get("reason"):
        row["note"] = str(interp.get("reason"))[:100]
    if "death" in types:
        row["died"] = True
    return row


def _summarize_game(
    clips: list[dict[str, Any]],
    *,
    report: dict[str, Any],
    export: dict[str, Any],
) -> dict[str, Any]:
    event_counts: dict[str, int] = {}
    themes: dict[str, int] = {}
    died = 0
    for clip in clips:
        for ev in clip.get("events") or []:
            event_counts[str(ev)] = event_counts.get(str(ev), 0) + 1
        if clip.get("died"):
            died += 1
        theme = str(clip.get("theme") or "").strip()
        if theme:
            themes[theme] = themes.get(theme, 0) + 1

    def _top(counter: dict[str, int], n: int = 4) -> list[str]:
        return [k for k, _ in sorted(counter.items(), key=lambda row: (-row[1], row[0]))[:n]]

    return {
        "clipCount": report.get("clipCount") or export.get("clipCount") or len(clips),
        "themes": _top(themes) or _top(event_counts),
        "eventMix": _top(event_counts),
        "deathMoments": died,
    }


def build_title_context(
    *,
    weave_stem: str,
    export: dict[str, Any],
    clips: list[dict[str, Any]],
    classifications: dict[str, Any],
    selections: dict[str, Any] | None = None,
    vod_title: str | None,
    dataset: Path | None,
    weave_report: dict[str, Any] | None = None,
    user_context: str | None = None,
) -> dict[str, Any]:
    game_id = export.get("gameId")
    sel = selections or {}
    game_clips = [c for c in clips if c.get("gameId") == game_id]

    def _rating(c: dict[str, Any]) -> str:
        return str(c.get("rating") or (sel.get(c["id"]) or {}).get("rating") or "")

    picked = [c for c in game_clips if _rating(c) in {"godly", "excellent"}]
    source_clips = picked if picked else game_clips

    light_clips = [
        _clip_light(clip, classifications=classifications, selections=sel)
        for clip in source_clips
    ]
    light_clips.sort(
        key=lambda row: (
            {"godly": 0, "excellent": 1, "keep": 2}.get(str(row.get("rating") or ""), 9),
            str(row.get("gameTime") or ""),
        )
    )

    weave_name = f"{weave_stem}.mp4"
    report = (weave_report or {}).get(weave_name) or {}
    game_summary = _summarize_game(light_clips, report=report, export=export)

    notes = str(user_context or "").strip()
    payload: dict[str, Any] = {
        "matchup": {
            "champion": export.get("champion"),
            "opponent": export.get("opponentChampion"),
        },
    }
    if notes:
        payload["creatorAngle"] = notes
        payload["userNotes"] = notes
        payload["_guidance"] = (
            "Write all hooks around creatorAngle. Do not default to generic matchup survival or combo hooks."
        )
        # Light background only — angle is primary.
        payload["gameSummary"] = game_summary
    else:
        payload["gameSummary"] = game_summary
        if dataset is not None:
            portrait_dir = dataset / "lol_compilations_picks_portrait"
            if portrait_dir.is_dir():
                caption_lines = _read_caption_lines(portrait_dir, weave_stem)
                if caption_lines:
                    payload["streamVibe"] = " · ".join(caption_lines[:4])[:180]
        payload["clipDetails"] = light_clips[:5]
        payload["_internal"] = {
            "result": export.get("result"),
            "win": report.get("win") if "win" in report else export.get("win"),
        }
    return payload


def normalize_generation_result(parsed: dict[str, Any]) -> dict[str, Any]:
    hooks: dict[str, str] = {}
    options: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(style: str, raw: Any) -> None:
        text = str(raw or "").strip()
        if not text:
            return
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        hooks[style] = text
        options.append({"style": style, "text": text})

    for row in parsed.get("hooks") or []:
        if isinstance(row, dict):
            _add(str(row.get("style") or "hook"), row.get("text"))

    best = str(parsed.get("best_hook") or "").strip()
    if best:
        _add("best", best)

    if not options:
        raise RuntimeError("OpenAI returned no hook suggestions")

    best = best or options[0]["text"]
    ordered = [opt for opt in options if opt["text"] == best]
    ordered.extend(opt for opt in options if opt["text"] != best)
    suggestions = [opt["text"] for opt in ordered[:HOOK_COUNT]]
    hashtags = [str(h).strip().lstrip("#") for h in (parsed.get("hashtags") or []) if str(h).strip()]
    return {
        "hooks": hooks,
        "hookOptions": ordered[:HOOK_COUNT],
        "suggestions": suggestions,
        "bestHook": best,
        "bestReason": str(parsed.get("best_reason") or "").strip(),
        "hashtags": hashtags[:6],
    }


class TitleSuggestionProvider:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> None:
        self.api_key = (
            api_key
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("OPEN_API_KEY")
            or ""
        )
        self.model = (
            model
            or os.environ.get("TITLE_MODEL")
            or os.environ.get("OPENAI_TITLE_MODEL")
            or DEFAULT_MODEL
        )
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        env_timeout = os.environ.get("OPENAI_TIMEOUT")
        if timeout is not None:
            self.timeout = timeout
        elif env_timeout and env_timeout.strip():
            self.timeout = float(env_timeout)
        else:
            self.timeout = DEFAULT_TIMEOUT
        env_retries = os.environ.get("OPENAI_RETRIES")
        if retries is not None:
            self.retries = retries
        elif env_retries and env_retries.strip().isdigit():
            self.retries = int(env_retries)
        else:
            self.retries = DEFAULT_RETRIES

    def generate(self, context: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key.strip():
            raise RuntimeError("OPENAI_API_KEY is not set")
        creator_angle = str(context.get("creatorAngle") or context.get("userNotes") or "").strip()
        system = TITLE_SYSTEM_PROMPT
        if creator_angle:
            system = f"{TITLE_SYSTEM_PROMPT}\n\n{TITLE_USER_NOTES_PROMPT}"
        if creator_angle:
            user_content = (
                f"CREATOR ANGLE (every hook must riff on this): {creator_angle}\n\n"
                f"Full context:\n{json.dumps(context, ensure_ascii=False)}"
            )
        else:
            user_content = (
                "Generate hooks for this game compilation:\n"
                f"{json.dumps(context, ensure_ascii=False)}"
            )
        body: dict[str, Any] = {
            "model": self.model,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "portrait_hooks",
                    "strict": True,
                    "schema": TITLE_JSON_SCHEMA,
                },
            },
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
        }
        temp = os.environ.get("OPENAI_TEMPERATURE")
        if temp is not None and temp.strip() != "":
            body["temperature"] = float(temp)
        raw = self._post_json("/chat/completions", body)
        parsed = self._parse_completion(raw)
        return normalize_generation_result(parsed)

    def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(body).encode("utf-8")
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            req = urllib.request.Request(
                f"{self.base_url}{path}",
                data=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"OpenAI API error {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                last_exc = exc
                if attempt >= self.retries:
                    break
                time.sleep(min(6.0, 1.5 * attempt))
        raise RuntimeError(f"OpenAI API request failed: {last_exc}") from last_exc

    def _parse_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError("OpenAI API returned no choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            text = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        else:
            text = str(content or "")
        if not text.strip():
            parsed = message.get("parsed")
            if isinstance(parsed, dict):
                return parsed
            raise RuntimeError("OpenAI API returned empty content")
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise RuntimeError("OpenAI structured output must be a JSON object")
        return parsed
