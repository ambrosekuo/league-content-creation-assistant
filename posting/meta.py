"""Upload metadata for finished shorts: title, description, hashtags, post state.

The sidecar lives next to the mp4 as {stem}.post.json so it does not collide with
the music sidecar mix_portrait_music.py writes at {stem}.json. Platform results are
kept in the sidecar so re-runs skip anything already uploaded.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from asr import champion_display
from dataset_paths import game_only_matches

SCHEMA_VERSION = 1

# YouTube hard limits.
TITLE_MAX = 100
DESCRIPTION_MAX = 5000

ROLE_LABELS = {
    "TOP": "Top",
    "JUNGLE": "Jungle",
    "MIDDLE": "Mid",
    "BOTTOM": "Bot",
    "UTILITY": "Support",
}

PORTRAIT_DIRS = (
    "lol_compilations_picks_portrait",
    "lol_compilations_portrait",
)

PORTRAIT_SUFFIXES = ("_portrait_music", "_portrait_decorated", "_portrait")

WEAVE_STEM_RE = re.compile(
    r"^gam(?P<game>\d+)_(?P<champ>[a-z0-9]+)(?:_vs_(?P<opp>[a-z0-9]+))?(?:_(?P<result>win|loss))?$",
    re.I,
)

BASE_HASHTAGS = ("leagueoflegends", "lolclips", "shorts")
HASHTAG_MAX = 8


def now_stamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def weave_stem_for(video: Path) -> str:
    """gam08_leblanc_vs_fizz_loss_portrait_music.mp4 -> gam08_leblanc_vs_fizz_loss."""
    stem = video.stem
    for suffix in PORTRAIT_SUFFIXES:
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def parse_weave_stem(stem: str) -> dict[str, Any]:
    match = WEAVE_STEM_RE.match(stem)
    if not match:
        return {"gameIndex": None, "champion": None, "opponentChampion": None, "result": None}
    return {
        "gameIndex": int(match.group("game")),
        "champion": champion_display(match.group("champ")),
        "opponentChampion": champion_display(match.group("opp")) if match.group("opp") else None,
        "result": (match.group("result") or "").lower() or None,
    }


def role_label(team_position: str | None) -> str | None:
    key = str(team_position or "").strip().upper()
    return ROLE_LABELS.get(key)


def viewer_dir(dataset: Path) -> Path:
    """data/{day}_{vod}/ -> data/_viewer/{day}_{vod}/ (review state, never uploaded)."""
    return dataset.parent / "_viewer" / dataset.name


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_titles(dataset: Path | None) -> dict[str, Any]:
    """Selected upload titles from the review UI Titles tab."""
    if dataset is None:
        return {}
    return _read_json(viewer_dir(dataset) / "titles.json")


def role_for_game(dataset: Path | None, game_index: int | None) -> str | None:
    """Lane from the Riot teamPosition recorded on the game's clips."""
    if dataset is None or game_index is None:
        return None
    payload = _read_json(dataset / "lol_clips" / "clips.json")
    for clip in payload.get("clips") or []:
        if int(clip.get("gameIndex") or 0) != int(game_index):
            continue
        label = role_label(clip.get("teamPosition"))
        if label:
            return label
    return None


def default_title(
    champion: str | None,
    opponent: str | None,
    role: str | None = None,
) -> str:
    """Fallback when no title was picked in the review UI: LeBlanc vs Fizz Mid."""
    champ = (champion or "").strip()
    opp = (opponent or "").strip()
    lane = (role or "").strip()
    if champ and opp:
        head = f"{champ} vs {opp}"
    elif champ:
        head = champ
    else:
        head = "League of Legends"
    return f"{head} {lane}".strip() if lane else head


def default_hashtags(champion: str | None, opponent: str | None) -> list[str]:
    tags: list[str] = []
    for name in (champion, opponent):
        slug = re.sub(r"[^a-z0-9]", "", str(name or "").lower())
        if slug:
            tags.append(slug)
    tags.extend(BASE_HASHTAGS)
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags:
        if tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return out[:HASHTAG_MAX]


def clean_title(text: str) -> str:
    """YouTube rejects <> in titles and caps length at 100."""
    cleaned = re.sub(r"\s+", " ", str(text or "")).replace("<", "").replace(">", "").strip()
    return cleaned[:TITLE_MAX].strip()


def music_credit(video: Path) -> dict[str, Any]:
    """Track info from the mix_portrait_music.py sidecar, if the mp4 has music."""
    sidecar = video.with_suffix(".json")
    if not sidecar.is_file():
        return {}
    payload = _read_json(sidecar)
    track = str(payload.get("track") or "").strip()
    if not track:
        return {}
    return {"track": track, "name": str(payload.get("name") or track)}


def build_description(
    *,
    title: str,
    champion: str | None,
    opponent: str | None,
    role: str | None,
    hashtags: list[str],
    music: dict[str, Any] | None = None,
) -> str:
    lines: list[str] = []
    matchup = None
    if champion and opponent:
        matchup = f"{champion} vs {opponent}"
        if role:
            matchup += f" — {role} lane"
    lines.append(matchup or title)

    channel = (os.environ.get("TWITCH_CHANNEL") or "").strip()
    if channel:
        lines.append("")
        lines.append(f"Live on Twitch: twitch.tv/{channel}")

    if music and music.get("name"):
        lines.append("")
        lines.append(f"Music: {music['name']}")

    if hashtags:
        lines.append("")
        lines.append(" ".join(f"#{tag}" for tag in hashtags))

    return "\n".join(lines)[:DESCRIPTION_MAX]


def sidecar_path(video: Path) -> Path:
    return video.with_name(f"{video.stem}.post.json")


def read_sidecar(video: Path) -> dict[str, Any]:
    return _read_json(sidecar_path(video))


def write_sidecar(video: Path, record: dict[str, Any]) -> Path:
    dest = sidecar_path(video)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return dest


def title_from_review(titles: dict[str, Any], weave_stem: str) -> str:
    """Whatever was selected/approved in the Titles tab for this game."""
    rec = titles.get(weave_stem)
    if not isinstance(rec, dict):
        return ""
    selected = str(rec.get("selected") or "").strip()
    if selected:
        return selected
    suggestions = rec.get("suggestions") or []
    return str(suggestions[0]).strip() if suggestions else ""


def hashtags_from_review(titles: dict[str, Any], weave_stem: str) -> list[str]:
    rec = titles.get(weave_stem)
    if not isinstance(rec, dict):
        return []
    tags = [str(tag).lstrip("#").strip() for tag in (rec.get("hashtags") or [])]
    return [tag for tag in tags if tag][:HASHTAG_MAX]


def build_record(
    video: Path,
    *,
    dataset: Path | None = None,
    titles: dict[str, Any] | None = None,
    title: str | None = None,
    description: str | None = None,
    hashtags: list[str] | None = None,
) -> dict[str, Any]:
    """Merge fresh metadata over any existing sidecar, keeping platform results."""
    video = video.resolve()
    existing = read_sidecar(video)
    weave_stem = weave_stem_for(video)
    parsed = parse_weave_stem(weave_stem)
    titles = titles if titles is not None else load_titles(dataset)

    champion = parsed.get("champion")
    opponent = parsed.get("opponentChampion")
    role = existing.get("role") or role_for_game(dataset, parsed.get("gameIndex"))

    # A hand-typed title stays put; otherwise the Titles tab selection wins,
    # then whatever the sidecar already had, then the matchup fallback.
    picked = clean_title(title or "")
    source = "manual"
    if not picked and str(existing.get("titleSource") or "") == "manual":
        picked = clean_title(existing.get("title") or "")
        source = "manual"
    if not picked:
        picked = clean_title(title_from_review(titles, weave_stem))
        source = "review"
    if not picked:
        picked = clean_title(existing.get("title") or "")
        source = str(existing.get("titleSource") or "default")
    if not picked:
        picked = clean_title(default_title(champion, opponent, role))
        source = "default"

    tags = [str(tag).lstrip("#").strip() for tag in (hashtags or []) if str(tag).strip()]
    if not tags:
        tags = hashtags_from_review(titles, weave_stem)
    if not tags:
        tags = [str(tag).lstrip("#") for tag in (existing.get("hashtags") or [])]
    if not tags:
        tags = default_hashtags(champion, opponent)
    tags = tags[:HASHTAG_MAX]

    music = music_credit(video) or (existing.get("music") or {})
    body = str(description or "").strip() or build_description(
        title=picked,
        champion=champion,
        opponent=opponent,
        role=role,
        hashtags=tags,
        music=music,
    )

    record = dict(existing)
    record.update(
        {
            "schema_version": SCHEMA_VERSION,
            "weaveStem": weave_stem,
            "video": video.name,
            "title": picked,
            "titleSource": source,
            "description": body[:DESCRIPTION_MAX],
            "hashtags": tags,
            "champion": champion,
            "opponentChampion": opponent,
            "role": role,
            "result": parsed.get("result"),
            "gameIndex": parsed.get("gameIndex"),
            "updated_at": now_stamp(),
        }
    )
    if music:
        record["music"] = music
    record.setdefault("youtube", {})
    record.setdefault("tiktok", {})
    return record


def is_postable(path: Path) -> bool:
    """Final exports only: the music mix is the upload candidate."""
    if path.suffix.lower() != ".mp4" or not path.is_file():
        return False
    return path.stem.lower().endswith("_portrait_music")


def discover_shorts(dataset: Path, *, only: str = "", picks_only: bool = True) -> list[Path]:
    """Music-mixed portraits under lol_compilations_*_portrait/post/."""
    dirs = PORTRAIT_DIRS[:1] if picks_only else PORTRAIT_DIRS
    found: list[Path] = []
    for folder_name in dirs:
        folder = dataset / folder_name
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("post/*.mp4")) + sorted(folder.glob("*.mp4")):
            if not is_postable(path):
                continue
            if only and not game_only_matches(path.name, only):
                continue
            found.append(path)
    seen: set[Path] = set()
    out: list[Path] = []
    for path in found:
        key = path.resolve()
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def platform_state(record: dict[str, Any], platform: str) -> dict[str, Any]:
    rec = record.get(platform)
    return dict(rec) if isinstance(rec, dict) else {}


def already_posted(record: dict[str, Any], platform: str) -> bool:
    state = platform_state(record, platform)
    if platform == "youtube":
        return bool(state.get("videoId"))
    if platform == "tiktok":
        return bool(state.get("publishId")) and state.get("status") != "failed"
    return False
