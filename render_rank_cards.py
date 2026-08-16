#!/usr/bin/env python3
"""9:16 start/end rank cards and opening gameplay overlays for Shorts.

Start (deprecated as the portrait intro): static card — road line, champ,
matchup, GM LP START, day. Kept for future / manual renders.

End: LP ticker after GAME_END with optional sting. This is the portrait outro.
Wins use the magic sting; losses use negative_beeps.

Overlay: road-to-Challenger pills on the first seconds of gameplay. This is
the current portrait intro (see wrap_portrait).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from generate_lobby_card import (
    TIER_COLORS,
    TIER_SHORT,
    champ_key,
    champion_icon,
    champion_splash,
    champion_tile,
    loading_splash,
    tier_emblem,
    tier_wings,
)

ROOT = Path(__file__).resolve().parent
OUT_W = 1080
OUT_H = 1920
BG = (10, 12, 18)
GOLD = (255, 214, 90)
WHITE = (244, 244, 248)
MUTED = (180, 186, 198)
GREEN = (86, 214, 140)
RED = (255, 92, 92)


def _first_existing(*candidates: str) -> str:
    for path in candidates:
        if path and Path(path).is_file():
            return path
    return candidates[-1]


FONT_BLACK = _first_existing(
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
)
FONT_BOLD = _first_existing(
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)
FONT_REG = _first_existing(
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)

DEFAULT_FACE = ROOT / "assets" / "brand" / "face_leblanc_classic.png"
DEFAULT_END_FACE = ROOT / "assets" / "brand" / "heart_hands.png"
DEFAULT_END_STING = ROOT / "assets" / "stings" / "inbox" / "264981__renatalmar__sfx-magic.wav"
DEFAULT_END_STING_LOSS = (
    ROOT / "assets" / "stings" / "suggested" / "loss_253886_negative_beeps_wav.wav"
)
DEFAULT_OVERLAY_STING = (
    ROOT / "assets" / "stings" / "suggested" / "sparkle_511485_cartoon_wink_magic_sparkle_wav.wav"
)
STREAMERS_PATH = ROOT / "assets" / "brand" / "streamers.json"
PROS_PATH = ROOT / "assets" / "brand" / "pros.json"
# IGN tokens that usually mean "I stream / I make VODs".
STREAMER_NAME_TOKENS = {"ttv", "twitch", "twtv", "yt", "youtube", "kick", "tiktok"}
TWITCH_ARCHIVE_MAX_AGE_DAYS = 90
WIN_DELTA = 29
LOSS_DELTA = 31
# Bewitching LeBlanc — default rank-card backdrop for LeBlanc games.
BEWITCHING_LEBLANC_SKIN = 45


def resolve_bg_skin(champ: str, override: int | None = None) -> int:
    if override is not None:
        return int(override)
    if champ_key(champ).lower() == "leblanc":
        return BEWITCHING_LEBLANC_SKIN
    return 0


def format_day_label(raw: str, *, from_path: Path | None = None) -> str:
    """Match-record stamp, e.g. AUG 15/2026 — not a 'this Saturday' teaser."""
    text = (raw or "").strip()
    if text:
        return text.upper()
    if from_path is not None and from_path.is_file():
        dt = datetime.fromtimestamp(from_path.stat().st_mtime)
        return f"{dt.strftime('%b').upper()} {dt.day}/{dt.year}"
    return ""


def even(n: int) -> int:
    return int(n) - (int(n) % 2)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def font(path: str, size: int):
    from PIL import ImageFont

    try:
        return ImageFont.truetype(path, size)
    except OSError:
        try:
            return ImageFont.truetype(FONT_REG, size)
        except OSError:
            return ImageFont.load_default()


def fit_font(draw, text: str, path: str, start: int, max_width: int, min_size: int = 22):
    for size in range(start, min_size - 1, -2):
        f = font(path, size)
        bbox = draw.textbbox((0, 0), text, font=f)
        if bbox[2] - bbox[0] <= max_width:
            return f
    return font(path, min_size)


def text_size(draw, text: str, fnt) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=fnt)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def draw_centered_parts(
    draw,
    parts: list[tuple[str, Any, tuple[int, int, int]]],
    *,
    cy: int,
    width: int = OUT_W,
) -> None:
    """One baseline, mixed styles. parts = [(text, font, fill), ...]."""
    chunks: list[tuple[str, Any, tuple[int, int, int], int, int]] = []
    total = 0
    max_h = 0
    for text, fnt, fill in parts:
        if not text:
            continue
        tw, th = text_size(draw, text, fnt)
        chunks.append((text, fnt, fill, tw, th))
        total += tw
        max_h = max(max_h, th)
    x = (width - total) // 2
    for text, fnt, fill, tw, th in chunks:
        y = int(cy - th / 2)
        draw.text((x + 2, y + 2), text, font=fnt, fill=(0, 0, 0))
        draw.text((x, y), text, font=fnt, fill=fill)
        x += tw


def draw_centered(
    draw,
    text: str,
    *,
    cy: int,
    fnt,
    fill: tuple[int, int, int],
    width: int = OUT_W,
    shadow: bool = True,
    alpha: float = 1.0,
) -> None:
    if alpha <= 0.01 or not text:
        return
    tw, th = text_size(draw, text, fnt)
    x = (width - tw) // 2
    y = int(cy - th / 2)
    a = max(0, min(255, int(round(255 * alpha))))
    if shadow:
        draw.text((x + 2, y + 2), text, font=fnt, fill=(0, 0, 0, a))
    rgb = fill if len(fill) == 3 else fill[:3]
    draw.text((x, y), text, font=fnt, fill=rgb + (a,))


def crop_face_square(im):
    """Square crop, centered. Wide webcam stills sit on the lower face, no Ken Burns."""
    w, h = im.size
    if abs(w - h) < max(w, h) * 0.18:
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
    else:
        side = int(min(w, h) * 0.90)
        side = max(2, even(side))
        left = max(0, min(w - side, (w - side) // 2 + 24))
        top = max(0, h - side)
    return im.crop((left, top, left + side, top + side))


def circle_mask(size: int):
    from PIL import Image, ImageDraw

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((1, 1, size - 2, size - 2), fill=255)
    return mask


def paste_center(
    base,
    overlay,
    cx: int,
    cy: int,
    size: tuple[int, int] | None = None,
    *,
    alpha: float = 1.0,
    scale: float = 1.0,
) -> None:
    from PIL import Image

    if alpha <= 0.01:
        return
    ov = overlay
    if size is not None:
        ov = overlay.resize(size, Image.Resampling.LANCZOS)
    if abs(scale - 1.0) > 0.01:
        nw = max(2, even(int(ov.width * scale)))
        nh = max(2, even(int(ov.height * scale)))
        ov = ov.resize((nw, nh), Image.Resampling.LANCZOS)
    if alpha < 0.995:
        a = ov.getchannel("A").point(lambda p: int(p * alpha))
        ov.putalpha(a)
    x = int(cx - ov.width / 2)
    y = int(cy - ov.height / 2)
    base.alpha_composite(ov, (x, y))


def format_lp(lp: int | None) -> str:
    if lp is None:
        return ""
    return f"{int(lp):,} LP"


def _norm_id(text: str) -> str:
    return " ".join("".join(ch if ch.isalnum() or ch.isspace() else " " for ch in (text or "").lower()).split())


def _ascii_fold(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in folded if not unicodedata.combining(ch))


def _name_tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", _ascii_fold(text).lower()) if t]


def _load_named_catalog(path: Path, key: str) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return [row for row in (data.get(key) or []) if isinstance(row, dict)]


def load_streamer_catalog(path: Path | None = None) -> list[dict[str, Any]]:
    return _load_named_catalog(path or STREAMERS_PATH, "streamers")


def load_pro_catalog(path: Path | None = None) -> list[dict[str, Any]]:
    return _load_named_catalog(path or PROS_PATH, "pros")


def _catalog_aliases(entry: dict[str, Any]) -> list[str]:
    raw = [str(a) for a in (entry.get("names") or []) if str(a).strip()]
    for extra in (entry.get("id"), entry.get("display")):
        if extra and str(extra).strip():
            raw.append(str(extra))
    aliases: list[str] = []
    seen: set[str] = set()
    for item in raw:
        norm = _norm_id(item)
        collapsed = norm.replace(" ", "")
        for alias in (norm, collapsed):
            if alias and alias not in seen:
                seen.add(alias)
                aliases.append(alias)
    return aliases


def match_streamer(player: dict[str, Any], catalog: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Match a lobby player to a catalog row. Name aliases win; tag is only a tie-break."""
    name = _norm_id(str(player.get("name") or ""))
    collapsed = name.replace(" ", "")
    tag = _norm_id(str(player.get("tag") or ""))
    if not name:
        return None
    for entry in catalog:
        aliases = _catalog_aliases(entry)
        if name in aliases or collapsed in aliases:
            return entry
        tags = [_norm_id(str(t)) for t in (entry.get("tags") or []) if str(t).strip()]
        if tag and tag in tags and any(alias and (alias in collapsed or collapsed in alias) for alias in aliases):
            return entry
    return None


def streamer_handle_hint(player: dict[str, Any]) -> bool:
    """True when the Riot ID itself advertises Twitch/YouTube/Kick."""
    tokens = _name_tokens(str(player.get("name") or ""))
    return any(t in STREAMER_NAME_TOKENS for t in tokens)


def twitch_login_candidates(name: str) -> list[str]:
    """Turn a Riot game name into plausible Twitch logins."""
    tokens = _name_tokens(name)
    core = [t for t in tokens if t not in STREAMER_NAME_TOKENS]
    cands: list[str] = []
    for parts in (tokens, core):
        if not parts:
            continue
        joined = "".join(parts)
        if 4 <= len(joined) <= 25:
            cands.append(joined)
        if len(parts) >= 2:
            pair = (parts[0] + parts[1])[:25]
            if 4 <= len(pair):
                cands.append(pair)
    out: list[str] = []
    seen: set[str] = set()
    for login in cands:
        if login not in seen:
            seen.add(login)
            out.append(login)
    return out


def _twitch_app_headers() -> dict[str, str] | None:
    from env_loader import load_dotenv
    from list_vods import get_app_access_token

    load_dotenv()
    client_id = os.environ.get("TWITCH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None
    token = get_app_access_token(client_id, client_secret)
    return {
        "Client-ID": client_id,
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


def _helix_json(path: str, headers: dict[str, str]) -> dict[str, Any]:
    from list_vods import HELIX, http_json

    return dict(http_json(f"{HELIX}{path}", headers=headers) or {})


def _recent_archive(user_id: str, headers: dict[str, str]) -> dict[str, Any] | None:
    payload = _helix_json(f"/videos?user_id={user_id}&type=archive&first=1", headers)
    videos = payload.get("data") or []
    if not videos:
        return None
    raw = str(videos[0].get("created_at") or "")
    created = None
    if raw:
        try:
            created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            created = None
    if created is not None:
        age = datetime.now(timezone.utc) - created
        if age > timedelta(days=TWITCH_ARCHIVE_MAX_AGE_DAYS):
            return None
    return videos[0]


def lookup_twitch_streamers(players: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map Riot name#tag → a live Twitch channel when the IGN matches a real streamer."""
    wanted: list[tuple[str, dict[str, Any], str]] = []
    logins: list[str] = []
    seen_logins: set[str] = set()
    for player in players:
        name = str(player.get("name") or "").strip()
        if not name:
            continue
        key = f"{name}#{player.get('tag') or ''}"
        for login in twitch_login_candidates(name):
            wanted.append((login, player, key))
            if login not in seen_logins:
                seen_logins.add(login)
                logins.append(login)
    if not logins:
        return {}
    try:
        headers = _twitch_app_headers()
    except Exception as exc:
        print(f"[streamers] twitch lookup failed: {exc}", flush=True)
        return {}
    if headers is None:
        print("[streamers] twitch lookup skipped (no TWITCH_CLIENT_ID/SECRET)", flush=True)
        return {}
    try:
        qs = "&".join(f"login={login}" for login in logins)
        users = {
            str(row.get("login") or "").lower(): row
            for row in (_helix_json(f"/users?{qs}", headers).get("data") or [])
            if row.get("login")
        }
    except Exception as exc:
        print(f"[streamers] twitch lookup failed: {exc}", flush=True)
        return {}

    found: dict[str, dict[str, Any]] = {}
    for login, player, key in wanted:
        if key in found:
            continue
        user = users.get(login)
        if user is None:
            continue
        kind = str(user.get("broadcaster_type") or "").strip().lower()
        archive = None
        try:
            if kind in {"partner", "affiliate"}:
                pass
            else:
                archive = _recent_archive(str(user.get("id") or ""), headers)
                if archive is None:
                    continue
        except Exception:
            if kind not in {"partner", "affiliate"}:
                continue
        found[key] = {
            "id": login,
            "display": str(user.get("display_name") or login),
            "login": login,
            "broadcasterType": kind,
            "source": "twitch",
        }
    return found


def _player_ally(player: dict[str, Any], meta: dict[str, Any]) -> bool:
    ally = bool(player.get("win")) == bool((meta.get("me") or {}).get("win", True))
    if player.get("teamId") is not None and (meta.get("me") or {}).get("teamId") is not None:
        ally = int(player["teamId"]) == int(meta["me"]["teamId"])
    return ally


def _streamer_display(
    player: dict[str, Any],
    *,
    catalog_hit: dict[str, Any] | None,
    twitch_hit: dict[str, Any] | None,
) -> str:
    if catalog_hit and str(catalog_hit.get("display") or "").strip():
        return str(catalog_hit["display"]).strip()
    raw = str(player.get("name") or "").strip()
    cleaned_parts = [t for t in re.split(r"\s+", raw) if t and t.lower().strip("._-") not in STREAMER_NAME_TOKENS]
    cleaned = " ".join(cleaned_parts).strip() or raw
    twitch_display = str((twitch_hit or {}).get("display") or "").strip()
    if twitch_display and twitch_display.lower() != "".join(_name_tokens(cleaned)):
        return twitch_display
    label = cleaned or twitch_display or raw
    if label.islower():
        return label.title()
    return label


def _callout_priority(row: dict[str, Any]) -> tuple[int, int]:
    """Pros first, then Twitch partners. Allies before enemies in the same bucket."""
    kind = str(row.get("kind") or "")
    source = str(row.get("source") or "")
    if kind == "pro" or source == "pro":
        tier = 0
    else:
        tier = 1
    return (tier, 0 if row.get("ally") else 1)


def detect_streamers(
    meta: dict[str, Any],
    *,
    catalog: list[dict[str, Any]] | None = None,
    pros: list[dict[str, Any]] | None = None,
    lookup: bool = True,
    quiet: bool = False,
) -> list[dict[str, Any]]:
    """Pros and Twitch partners in this lobby. Affiliates and IGN-only hits are ignored."""
    entries = catalog if catalog is not None else load_streamer_catalog()
    pro_entries = pros if pros is not None else load_pro_catalog()
    players = [p for p in (meta.get("players") or []) if not p.get("mine")]
    twitch_map = lookup_twitch_streamers(players) if lookup else {}
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for player in players:
        pro_hit = match_streamer(player, pro_entries)
        catalog_hit = match_streamer(player, entries)
        key = f"{player.get('name') or ''}#{player.get('tag') or ''}"
        twitch_hit = twitch_map.get(key)
        btype = str((twitch_hit or {}).get("broadcasterType") or "")
        if pro_hit is None and btype != "partner":
            continue
        display = _streamer_display(
            player,
            catalog_hit=pro_hit or catalog_hit,
            twitch_hit=twitch_hit,
        )
        sid = str(
            (pro_hit or {}).get("id")
            or (catalog_hit or {}).get("id")
            or (twitch_hit or {}).get("id")
            or display
            or player.get("name")
            or ""
        ).lower()
        if sid in seen:
            continue
        seen.add(sid)
        if pro_hit is not None:
            kind, source = "pro", "pro"
        elif catalog_hit is not None:
            kind, source = "streamer", "catalog"
        elif twitch_hit is not None:
            kind, source = "streamer", "twitch"
        else:
            kind, source = "streamer", "handle"
        found.append(
            {
                "id": sid,
                "display": display,
                "champion": str(player.get("champion") or ""),
                "name": str(player.get("name") or ""),
                "tag": str(player.get("tag") or ""),
                "ally": _player_ally(player, meta),
                "position": str(player.get("position") or ""),
                "kind": kind,
                "org": str((pro_hit or {}).get("org") or ""),
                "source": source,
                "login": (twitch_hit or {}).get("login"),
                "broadcasterType": (twitch_hit or {}).get("broadcasterType") or "",
            }
        )
    found.sort(key=_callout_priority)
    if not quiet:
        if found:
            summary = ", ".join(
                f"{row['display']} ({'ally' if row.get('ally') else 'enemy'} "
                f"{row.get('champion') or '?'} · {row.get('kind') or row.get('source')}"
                f"{' ' + row['broadcasterType'] if row.get('broadcasterType') else ''})"
                for row in found
            )
            print(f"[lobby] {len(found)} notables: {summary}", flush=True)
        else:
            print("[lobby] no pros or streamers detected", flush=True)
    return found


def resolve_card(
    meta: dict[str, Any],
    *,
    identity_tier: str,
    tagline: str,
    handle: str,
    cta: str,
    start_lp: int | None,
    end_lp: int | None,
    lp_delta: int | None,
    estimate_lp: bool,
    peak_lp: int | None = None,
    date_label: str = "",
    road_line: str = "ROAD BACK UP TO CHALLENGER",
) -> dict[str, Any]:
    me = dict(meta.get("me") or {})
    hook = dict(meta.get("hook") or {})
    champ_raw = str(me.get("champion") or hook.get("meChampion") or "Unknown")
    champ = champ_raw.upper()
    play_tier = str(me.get("tier") or hook.get("meTier") or "UNRANKED").upper()
    win = me.get("win")
    if win is None:
        win = True
    else:
        win = bool(win)

    meta_lp = me.get("lp")
    if meta_lp is None:
        meta_lp = hook.get("meLp")
    if meta_lp is not None:
        meta_lp = int(meta_lp)

    estimated = False
    if end_lp is None and start_lp is None:
        end_lp = meta_lp
        if estimate_lp and end_lp is not None:
            delta = WIN_DELTA if win else -LOSS_DELTA
            start_lp = max(0, end_lp - delta)
            estimated = True
        else:
            start_lp = meta_lp
    elif end_lp is None:
        end_lp = start_lp + (WIN_DELTA if win else -LOSS_DELTA) if start_lp is not None else meta_lp
        estimated = True
    elif start_lp is None:
        start_lp = end_lp - (WIN_DELTA if win else -LOSS_DELTA) if end_lp is not None else meta_lp
        estimated = True

    if lp_delta is None and start_lp is not None and end_lp is not None:
        lp_delta = int(end_lp) - int(start_lp)
    ident = (identity_tier or play_tier).upper()
    ident_short = TIER_SHORT.get(ident, ident)
    play_short = TIER_SHORT.get(play_tier, play_tier)
    opp = str(me.get("laneOpponent") or hook.get("oppChampion") or "").upper()
    if not opp:
        opp = str((meta.get("opponent") or {}).get("champion") or "").upper()
    vs_line = f"vs  {opp}" if opp else ""
    display_lp = meta_lp if meta_lp is not None else start_lp
    road = (road_line or "ROAD BACK UP TO CHALLENGER").strip()
    road_top, road_bot = road, ""
    marker = " TO "
    idx = road.upper().rfind(marker)
    if idx > 0:
        road_top = road[:idx].strip()
        road_bot = road[idx + 1 :].strip()  # keeps "TO CHALLENGER"
    return {
        "champion": champ,
        "championKey": champ_raw,
        "identityTier": ident,
        "identityShort": ident_short,
        "playTier": play_tier,
        "playShort": play_short,
        "win": win,
        "startLp": start_lp,
        "endLp": end_lp,
        "lpDelta": lp_delta,
        "lpEstimated": estimated,
        "tagline": tagline,
        "handle": handle,
        "cta": cta,
        "dateLabel": date_label,
        "roadTop": road_top,
        "roadBot": road_bot,
        "startHeadline": road_bot or road_top or ident,
        "startChamp": champ,
        "oppChamp": opp,
        "vsLine": vs_line,
        "displayLp": display_lp,
        "endDelta": (
            f"+{int(lp_delta)} LP"
            if lp_delta is not None and lp_delta > 0
            else f"{int(lp_delta)} LP"
            if lp_delta is not None and lp_delta < 0
            else ("WIN" if win else "LOSS")
        ),
        "endRank": play_tier,
        "endLpLabel": format_lp(end_lp),
        "startLpLabel": (
            f"START: {play_short} - {format_lp(display_lp)}".strip()
            if display_lp is not None
            else f"START: {play_short}"
        ),
        "peakLp": peak_lp,
        "peakLabel": f"{format_lp(peak_lp)} PEAK" if peak_lp is not None else "",
    }


def negative_lp(card: dict[str, Any]) -> bool:
    if card.get("win") is False:
        return True
    delta = card.get("lpDelta")
    if delta is not None:
        return int(delta) < 0
    return not bool(card.get("win"))


def default_end_sting(
    card: dict[str, Any],
    *,
    win_sting: Path | None = None,
    loss_sting: Path | None = None,
) -> Path | None:
    win_path = win_sting if win_sting is not None else DEFAULT_END_STING
    loss_path = loss_sting if loss_sting is not None else DEFAULT_END_STING_LOSS
    chosen = loss_path if negative_lp(card) else win_path
    if chosen is not None and chosen.is_file():
        return chosen
    if win_path is not None and win_path.is_file():
        return win_path
    return None


def ticker_progress(t: float, *, start_lp: int, end_lp: int, fps: float) -> float:
    """0 at hold, 1 when the LP count finishes. One LP per frame, eased out."""
    hold = 0.12
    if t <= hold:
        return 0.0
    delta = abs(int(end_lp) - int(start_lp))
    count_dur = max(0.70, min(1.55, max(delta, 1) / max(fps, 1.0)))
    u = min(1.0, (t - hold) / count_dur)
    return 1.0 - (1.0 - u) ** 2


def ticker_values(card: dict[str, Any], t: float, *, fps: float) -> tuple[int, int, float, float]:
    """Return (shown_lp, shown_delta, delta_scale, progress)."""
    start = int(card.get("startLp") or 0)
    end = int(card.get("endLp") if card.get("endLp") is not None else start)
    prog = ticker_progress(t, start_lp=start, end_lp=end, fps=fps)
    shown = start + int(round((end - start) * prog))
    delta = shown - start
    punch = 1.0
    if prog >= 1.0:
        hold = 0.12
        count_dur = max(0.70, min(1.55, max(abs(end - start), 1) / max(fps, 1.0)))
        over = t - (hold + count_dur)
        punch = 1.0 + 0.12 * max(0.0, 1.0 - over / 0.22)
    return shown, delta, punch, prog


def reveal(t: float | None, at: float, dur: float = 0.16) -> tuple[float, float]:
    """Pop-in: (alpha, scale). t=None means the rest pose (fully shown, scale 1)."""
    if t is None:
        return 1.0, 1.0
    if t < at:
        return 0.0, 0.78
    u = min(1.0, (t - at) / max(dur, 0.01))
    e = 1.0 - (1.0 - u) ** 3
    alpha = min(1.0, e * 1.2)
    if e < 0.72:
        scale = 0.78 + 0.30 * (e / 0.72)
    else:
        scale = 1.08 - 0.08 * ((e - 0.72) / 0.28)
    return min(1.0, alpha), scale


def render_background(champ: str, *, skin: int = 0):
    from PIL import Image, ImageEnhance, ImageFilter

    canvas = Image.new("RGBA", (OUT_W, OUT_H), BG + (255,))
    art = champion_splash(champ, skin)
    if art is None:
        art = loading_splash(champ, skin) or loading_splash(champ, 0)
    if art is not None:
        ratio = max(OUT_W / art.width, OUT_H / art.height)
        # Landscape splash already covers 9:16 by height; extra zoom clips the hat.
        zoom = 1.0 if art.width >= art.height else 1.08
        sw = even(int(art.width * ratio * zoom))
        sh = even(int(art.height * ratio * zoom))
        bg = art.resize((sw, sh), Image.Resampling.LANCZOS)
        bx = (OUT_W - sw) // 2
        by = (OUT_H - sh) // 2
        dim = ImageEnhance.Brightness(bg).enhance(0.42)
        canvas.alpha_composite(dim, (bx, by))
        canvas = ImageEnhance.Color(canvas).enhance(0.82)
        canvas = canvas.filter(ImageFilter.GaussianBlur(radius=0.8))
    scrim = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0))
    from PIL import ImageDraw

    d = ImageDraw.Draw(scrim)
    for i in range(220):
        a = int(160 * (1 - i / 220) ** 1.1)
        d.line([(0, i), (OUT_W, i)], fill=(8, 10, 16, a))
    for i in range(420):
        a = int(210 * (1 - i / 420) ** 1.05)
        y = OUT_H - 1 - i
        d.line([(0, y), (OUT_W, y)], fill=(8, 10, 16, a))
    return Image.alpha_composite(canvas.convert("RGBA"), scrim)


def face_layer(face_im, size: int, ring_color: tuple[int, int, int]):
    from PIL import Image, ImageDraw

    square = crop_face_square(face_im.convert("RGBA"))
    portrait = square.resize((size, size), Image.Resampling.LANCZOS)
    layer = Image.new("RGBA", (size + 28, size + 28), (0, 0, 0, 0))
    ring = ImageDraw.Draw(layer)
    ring.ellipse((0, 0, size + 27, size + 27), fill=ring_color + (255,))
    ring.ellipse((8, 8, size + 19, size + 19), fill=(12, 14, 20, 255))
    portrait.putalpha(circle_mask(size))
    layer.alpha_composite(portrait, (14, 14))
    return layer


def render_card(
    card: dict[str, Any],
    face_im,
    *,
    kind: str,
    t: float | None = None,
    shown_lp: int | None = None,
    shown_delta: int | None = None,
    delta_scale: float = 1.0,
):
    from PIL import Image, ImageDraw

    canvas = render_background(
        str(card.get("championKey") or card["champion"]),
        skin=int(card.get("bgSkin") or 0),
    )
    draw = ImageDraw.Draw(canvas)
    cx = OUT_W // 2
    play_color = TIER_COLORS.get(card["playTier"], TIER_COLORS["UNRANKED"])
    ident_color = TIER_COLORS.get(card["identityTier"], GOLD)

    crest_tier = card["identityTier"] if kind == "start" else card["playTier"]
    crest_color = ident_color if kind == "start" else play_color
    emblem = tier_emblem(crest_tier)
    wings = tier_wings(crest_tier)

    if kind == "start":
        face_size, face_cy = 460, 470
        wing_size, emblem_size, emblem_y = (560, 168), (176, 176), 108
        wing_cy = face_cy + 186
    else:
        face_size, face_cy = 520, 540
        wing_size, emblem_size, emblem_y = (640, 200), (220, 220), 132
        wing_cy = face_cy + 188
    face = face_layer(face_im, face_size, crest_color)
    paste_center(canvas, face, cx, face_cy)

    if wings is not None:
        paste_center(canvas, wings, cx, wing_cy, size=wing_size)
    if emblem is not None:
        paste_center(canvas, emblem, cx, emblem_y, size=emblem_size)

    if kind == "start":
        handle = str(card.get("handle") or "")
        f_name = fit_font(draw, handle, FONT_BOLD, 44, OUT_W - 120, 26)
        draw_centered(draw, handle, cy=798, fnt=f_name, fill=WHITE)

        road_top = str(card.get("roadTop") or "").upper()
        road_bot = str(card.get("roadBot") or "").upper()
        road_lines = [line for line in (road_top, road_bot) if line]
        if not road_lines:
            headline = str(card.get("startHeadline") or "").upper()
            if headline:
                road_lines = [headline]
        f_road = font(FONT_BLACK, 52)
        if road_lines:
            longest = max(road_lines, key=len)
            f_road = fit_font(draw, longest, FONT_BLACK, 54, OUT_W - 72, 32)
        y_road = 930
        for i, line in enumerate(road_lines):
            draw_centered(draw, line, cy=y_road + i * 78, fnt=f_road, fill=GOLD)

        pill = str(card.get("startLpLabel") or "")
        if pill:
            f_lp = fit_font(draw, pill, FONT_BOLD, 34, OUT_W - 80, 22)
            draw_centered(draw, pill, cy=1148, fnt=f_lp, fill=WHITE)

        champ = str(card.get("startChamp") or "")
        opp = str(card.get("vsLine") or "").replace("vs", "").strip()
        day = str(card.get("dateLabel") or "")
        f_vs = font(FONT_BOLD, 26)
        parts: list[tuple[str, Any, tuple[int, int, int]]] = []
        if champ:
            parts.append((champ, f_vs, WHITE))
        if opp:
            parts.append(("  VS  ", f_vs, MUTED))
            parts.append((opp, f_vs, RED))
        if parts:
            draw_centered_parts(draw, parts, cy=1256)
        if day:
            f_day = fit_font(draw, day, FONT_BOLD, 24, OUT_W - 200, 16)
            draw_centered(draw, day, cy=1336, fnt=f_day, fill=MUTED)
    else:
        delta_n = int(shown_delta if shown_delta is not None else (card.get("lpDelta") or 0))
        if delta_n > 0:
            delta = f"+{delta_n} LP"
        elif delta_n < 0:
            delta = f"{delta_n} LP"
        else:
            delta = "+0 LP" if card["win"] else "0 LP"
        lp_now = shown_lp if shown_lp is not None else card.get("endLp")
        delta_fill = GREEN if (delta_n > 0 or card["win"]) else RED
        size = int(round(92 * max(1.0, float(delta_scale))))
        f_d = fit_font(draw, delta, FONT_BLACK, size, OUT_W - 60, 36)
        draw_centered(draw, delta, cy=900, fnt=f_d, fill=delta_fill)
        f_r = fit_font(draw, card["endRank"], FONT_BLACK, 64, OUT_W - 80, 32)
        draw_centered(draw, card["endRank"], cy=1010, fnt=f_r, fill=GOLD)
        lp_label = format_lp(int(lp_now) if lp_now is not None else None)
        if lp_label:
            f_lp = fit_font(draw, lp_label, FONT_BOLD, 52, OUT_W - 100, 26)
            draw_centered(draw, lp_label, cy=1108, fnt=f_lp, fill=WHITE)
        f_tag = fit_font(draw, card["tagline"], FONT_BOLD, 36, OUT_W - 120, 22)
        draw_centered(draw, card["tagline"], cy=1210, fnt=f_tag, fill=MUTED)
        f_cta = fit_font(draw, card["cta"], FONT_BOLD, 34, OUT_W - 100, 18)
        draw_centered(draw, card["cta"], cy=1760, fnt=f_cta, fill=WHITE)

    return canvas.convert("RGB")


def write_clip(
    card: dict[str, Any],
    face_im,
    output: Path,
    *,
    kind: str,
    seconds: float,
    fps: float = 30.0,
    sting: Path | None = None,
) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    rate = max(24, min(60, int(round(fps))))
    frames = max(1, int(round(seconds * rate)))
    with tempfile.TemporaryDirectory(prefix=f"rank_{kind}_") as tmp:
        tmp_dir = Path(tmp)
        if kind == "start":
            render_card(card, face_im, kind="start").save(tmp_dir / "f0000.jpg", quality=92)
            cmd = [
                "ffmpeg",
                "-y",
                "-loop",
                "1",
                "-framerate",
                str(rate),
                "-i",
                str(tmp_dir / "f0000.jpg"),
                "-t",
                f"{seconds:.2f}",
            ]
        else:
            for i in range(frames):
                t = i / rate
                shown, delta, punch, _prog = ticker_values(card, t, fps=float(rate))
                frame = render_card(
                    card,
                    face_im,
                    kind="end",
                    t=t,
                    shown_lp=shown,
                    shown_delta=delta,
                    delta_scale=punch,
                )
                frame.save(tmp_dir / f"f{i:04d}.jpg", quality=92)
            cmd = [
                "ffmpeg",
                "-y",
                "-framerate",
                str(rate),
                "-i",
                str(tmp_dir / "f%04d.jpg"),
            ]
        if sting is not None and sting.is_file():
            fade = max(0.05, float(seconds) - 0.22)
            cmd += [
                "-i",
                str(sting),
                "-filter_complex",
                (
                    f"[1:a]aformat=sample_fmts=fltp:sample_rates=48000:"
                    f"channel_layouts=stereo,volume=0.85,apad,"
                    f"atrim=0:{seconds:.3f},afade=t=out:st={fade:.3f}:d=0.20[a]"
                ),
                "-map",
                "0:v",
                "-map",
                "[a]",
            ]
        else:
            cmd += [
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-shortest",
            ]
        cmd += [
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "18",
            "-preset",
            "veryfast",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(output),
        ]
        proc = subprocess.run(cmd, text=True, capture_output=True)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"ffmpeg {kind} card failed\n{detail}")
    return output


OVERLAY_VARIANTS = ("vs_lp", "one_line", "lp_only", "road_lp")


def overlay_lp_line(card: dict[str, Any], *, start_label: bool = False) -> str:
    lp = format_lp(card.get("displayLp") if card.get("displayLp") is not None else card.get("endLp") or card.get("startLp"))
    rank = str(card.get("playShort") or "").strip()
    if start_label:
        parts = [p for p in (rank, lp, "START") if p]
        return " ".join(parts)
    if rank and lp:
        return f"{rank}  ·  {lp}"
    return lp or rank


def overlay_spec(card: dict[str, Any], variant: str) -> dict[str, Any]:
    champ = str(card.get("startChamp") or "").upper()
    opp = str(card.get("oppChamp") or "").upper()
    lp = overlay_lp_line(card)
    if variant == "one_line":
        mid = f"{champ}  VS  {opp}" if opp else champ
        line = f"{mid}  ·  {lp}" if lp else mid
        return {"variant": variant, "lines": [("one", line)]}
    if variant == "lp_only":
        return {"variant": variant, "lines": [("lp", lp)]}
    if variant == "road_lp":
        return {
            "variant": variant,
            "lines": [
                ("road", "ROAD BACK TO CHALLENGER"),
                ("lp", overlay_lp_line(card, start_label=True)),
            ],
        }
    return {"variant": variant, "lines": [("vs", champ, opp), ("lp", lp)]}


def _line_size(draw, text: str, fnt) -> tuple[int, int]:
    return text_size(draw, text, fnt)


DEFAULT_CHALLENGER_CUTOFF = 1524
CHALLENGER_SLOTS = 300


def _league_lps(league: dict[str, Any]) -> list[int]:
    return [
        int(entry["leaguePoints"])
        for entry in (league.get("entries") or [])
        if entry.get("leaguePoints") is not None
    ]


def fetch_challenger_cutoff(*, platform: str = "na1", slots: int = CHALLENGER_SLOTS) -> int | None:
    """LP of the Nth player on the live Master+ ladder (NA Challenger = 300).

    min(Challenger-tagged LP) is wrong: people can lose after the daily
    apex update and still wear Challenger below the real entry line.
    """
    import os

    from env_loader import load_dotenv

    load_dotenv()
    key = os.environ.get("RIOT_API_KEY", "").strip()
    if not key:
        return None
    try:
        sys.path.insert(0, str(ROOT / "lol-indexer"))
        from riot_api import RiotAPI

        api = RiotAPI(key)
        chall = api.get_challenger_league(platform=platform)
        gm = api.get_grandmaster_league(platform=platform)
    except Exception:
        return None
    ranked = sorted(_league_lps(chall) + _league_lps(gm), reverse=True)
    if len(ranked) < slots:
        return ranked[-1] if ranked else None
    return ranked[slots - 1]


def resolve_cutoff_lp(card: dict[str, Any]) -> int | None:
    raw = card.get("challengerCutoffLp")
    if raw is None:
        return None
    return int(raw)


def render_road_progress_overlay(
    card: dict[str, Any],
    *,
    cy: int = 1180,
    opacity: float = 0.90,
):
    """Headline + current LP / Challenger-cutoff bar, centered."""
    from PIL import Image, ImageDraw

    canvas = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    now = card.get("displayLp")
    if now is None:
        now = card.get("endLp") or card.get("startLp")
    cutoff = resolve_cutoff_lp(card)
    if cutoff is None:
        cutoff = fetch_challenger_cutoff(
            platform=str(card.get("cutoffPlatform") or "na1")
        ) or DEFAULT_CHALLENGER_CUTOFF
        card["challengerCutoffLp"] = cutoff
    now_i = int(now or 0)
    frac = 0.0 if cutoff <= 0 else max(0.0, min(1.0, now_i / float(cutoff)))

    headline = "ROAD BACK TO CHALLENGER"
    sub = str(card.get("overlaySub") or "RANK 1 LEBLANC NA").strip()
    f_head = fit_font(draw, headline, FONT_BLACK, 50, OUT_W - 120, 28)
    hw, hh = text_size(draw, headline, f_head)
    now_label = format_lp(now_i)
    cut_label = format_lp(cutoff)
    f_lp = fit_font(draw, now_label, FONT_BLACK, 36, 280, 20)
    nw, nh = text_size(draw, now_label, f_lp)
    cw, ch = text_size(draw, cut_label, f_lp)

    play_emblem = tier_emblem(str(card.get("playTier") or "GRANDMASTER"))
    ident_emblem = tier_emblem(str(card.get("identityTier") or "CHALLENGER"))
    icon_h = even(max(36, nh + 4))
    gm_w = gm_h = 0
    ch_w = ch_h = 0
    if play_emblem is not None:
        scale = icon_h / max(play_emblem.height, 1)
        gm_w = even(max(2, int(play_emblem.width * scale)))
        gm_h = even(max(2, int(play_emblem.height * scale)))
    if ident_emblem is not None:
        scale = icon_h / max(ident_emblem.height, 1)
        ch_w = even(max(2, int(ident_emblem.width * scale)))
        ch_h = even(max(2, int(ident_emblem.height * scale)))

    bar_w, bar_h = 360, 18
    gap_x = 14
    row_w = gm_w + (12 if gm_w else 0) + nw + gap_x + bar_w + gap_x + cw + (12 if ch_w else 0) + ch_w
    row_h = max(gm_h, nh, bar_h, ch, ch_h)
    line_gap = 40
    block_w = max(hw, row_w)
    block_h = hh + line_gap + row_h
    y0 = int(cy - block_h / 2)
    pad_x, pad_y = 52, 40
    ImageDraw.Draw(canvas).rounded_rectangle(
        (
            (OUT_W - block_w) // 2 - pad_x,
            y0 - pad_y,
            (OUT_W + block_w) // 2 + pad_x,
            y0 + block_h + pad_y,
        ),
        radius=32,
        fill=(8, 10, 16, 150),
    )
    draw_centered(draw, headline, cy=y0 + hh // 2, fnt=f_head, fill=GOLD)

    mid = y0 + hh + line_gap + row_h // 2
    x = (OUT_W - row_w) // 2
    if play_emblem is not None and gm_w:
        paste_center(canvas, play_emblem, x + gm_w // 2, mid, size=(gm_w, gm_h))
        x += gm_w + 12
    draw.text((x + 2, mid + 2), now_label, font=f_lp, fill=(0, 0, 0), anchor="lm")
    draw.text((x, mid), now_label, font=f_lp, fill=WHITE, anchor="lm")
    x += nw + gap_x
    track = (255, 255, 255, 46)
    by0 = mid - bar_h // 2
    draw.rounded_rectangle((x, by0, x + bar_w, by0 + bar_h), radius=bar_h // 2, fill=track)
    fill_w = max(bar_h, int(round(bar_w * frac)))
    draw.rounded_rectangle((x, by0, x + fill_w, by0 + bar_h), radius=bar_h // 2, fill=GOLD + (255,))
    x += bar_w + gap_x
    draw.text((x + 2, mid + 2), cut_label, font=f_lp, fill=(0, 0, 0), anchor="lm")
    draw.text((x, mid), cut_label, font=f_lp, fill=GOLD, anchor="lm")
    x += cw + 12
    if ident_emblem is not None and ch_w:
        paste_center(canvas, ident_emblem, x + ch_w // 2, mid, size=(ch_w, ch_h))

    if sub:
        f_sub = fit_font(draw, sub, FONT_BLACK, 56, OUT_W - 160, 28)
        sw, sh = text_size(draw, sub, f_sub)
        champ_name = str(card.get("championKey") or card.get("startChamp") or "")
        skin = int(card.get("bgSkin") or resolve_bg_skin(champ_name))
        me_art = champion_tile(champ_name, skin) or champion_icon(champ_name)
        icon = champ_badge(me_art, 88, GOLD, radius=16) if me_art is not None else None
        icon_gap = 16 if icon is not None else 0
        badge_w = sw + (icon.width + icon_gap if icon is not None else 0)
        badge_h = max(sh, icon.height if icon is not None else 0)
        sub_cy = int(card.get("overlaySubY") or 740)
        bx0 = (OUT_W - badge_w) // 2 - 36
        by0 = sub_cy - badge_h // 2 - 20
        ImageDraw.Draw(canvas).rounded_rectangle(
            (bx0, by0, bx0 + badge_w + 72, by0 + badge_h + 40),
            radius=28,
            fill=(8, 10, 16, 150),
        )
        bx = (OUT_W - badge_w) // 2
        if icon is not None:
            paste_center(canvas, icon, bx + icon.width // 2, sub_cy)
            bx += icon.width + icon_gap
        draw.text((bx + 2, sub_cy + 2), sub, font=f_sub, fill=(0, 0, 0), anchor="lm")
        draw.text((bx, sub_cy), sub, font=f_sub, fill=GOLD, anchor="lm")

    draw_streamer_callouts(canvas, card.get("streamers") or [])

    opacity = max(0.15, min(1.0, float(opacity)))
    if opacity < 0.995:
        alpha = canvas.getchannel("A").point(lambda p: int(p * opacity))
        canvas.putalpha(alpha)
    return canvas


def draw_streamer_callouts(
    canvas,
    streamers: list[dict[str, Any]],
    *,
    cy: int = 1520,
    tile: int = 176,
) -> None:
    """Big champ tiles + names for known streamers in this lobby."""
    from PIL import ImageDraw

    if not streamers:
        return
    draw = ImageDraw.Draw(canvas)
    gap = 36
    label_gap = 14
    cards: list[dict[str, Any]] = []
    for row in streamers[:3]:
        champ = str(row.get("champion") or "")
        art = champion_tile(champ, 0) or champion_icon(champ)
        if art is None:
            continue
        ally = bool(row.get("ally"))
        ring = GOLD if ally else RED
        badge = champ_badge(art, tile, ring, radius=22)
        label = str(row.get("display") or row.get("name") or "").upper()
        if str(row.get("kind") or "") == "pro":
            side = "PRO"
        else:
            side = "ALLY" if ally else "ENEMY"
        f_name = fit_font(draw, label, FONT_BLACK, 36, 280, 20)
        f_side = font(FONT_BOLD, 18)
        nw, nh = text_size(draw, label, f_name)
        sw, sh = text_size(draw, side, f_side)
        width = max(badge.width, nw, sw)
        height = badge.height + 8 + sh + 6 + nh
        cards.append(
            {
                "badge": badge,
                "label": label,
                "side": side,
                "ring": ring,
                "f_name": f_name,
                "f_side": f_side,
                "nw": nw,
                "nh": nh,
                "sw": sw,
                "sh": sh,
                "w": width,
                "h": height,
            }
        )
    if not cards:
        return
    row_w = sum(c["w"] for c in cards) + gap * (len(cards) - 1)
    row_h = max(c["h"] for c in cards)
    x0 = (OUT_W - row_w) // 2
    y0 = int(cy - row_h / 2)
    pad_x, pad_y = 36, 28
    ImageDraw.Draw(canvas).rounded_rectangle(
        (x0 - pad_x, y0 - pad_y, x0 + row_w + pad_x, y0 + row_h + pad_y),
        radius=28,
        fill=(8, 10, 16, 150),
    )
    x = x0
    for card in cards:
        cx = x + card["w"] // 2
        paste_center(canvas, card["badge"], cx, y0 + card["badge"].height // 2)
        side_y = y0 + card["badge"].height + 8 + card["sh"] // 2
        name_y = side_y + card["sh"] // 2 + 6 + card["nh"] // 2
        draw.text((cx + 2, side_y + 2), card["side"], font=card["f_side"], fill=(0, 0, 0), anchor="mm")
        draw.text((cx, side_y), card["side"], font=card["f_side"], fill=card["ring"], anchor="mm")
        draw.text((cx + 2, name_y + 2), card["label"], font=card["f_name"], fill=(0, 0, 0), anchor="mm")
        draw.text((cx, name_y), card["label"], font=card["f_name"], fill=WHITE, anchor="mm")
        x += card["w"] + gap


def champ_badge(im, size: int, ring: tuple[int, int, int], radius: int = 20):
    from PIL import Image, ImageDraw

    pad = 5
    outer = size + pad * 2
    layer = Image.new("RGBA", (outer, outer), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle((0, 0, outer - 1, outer - 1), radius=radius + 2, fill=ring + (255,))
    d.rounded_rectangle((2, 2, outer - 3, outer - 3), radius=radius + 1, fill=(12, 14, 20, 255))
    icon = im.resize((size, size), Image.Resampling.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    icon.putalpha(mask)
    layer.alpha_composite(icon, (pad, pad))
    return layer


def render_overlay_png(
    card: dict[str, Any],
    variant: str,
    *,
    cy: int = 1180,
    opacity: float = 0.90,
):
    """Transparent 9:16 PNG: champ/rank icons + LP on a see-through pill."""
    from PIL import Image, ImageDraw

    if variant == "road_lp":
        return render_road_progress_overlay(card, cy=cy, opacity=opacity)

    spec = overlay_spec(card, variant)
    canvas = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    max_w = OUT_W - 96
    me_art = champion_icon(str(card.get("championKey") or card.get("startChamp") or ""))
    opp_art = champion_icon(str(card.get("oppChamp") or ""))
    play_emblem = tier_emblem(str(card.get("playTier") or ""))
    ident_emblem = tier_emblem(str(card.get("identityTier") or ""))

    prepared: list[dict[str, Any]] = []
    for item in spec["lines"]:
        kind = item[0]
        if kind == "vs" and me_art is not None:
            icon_size = 100
            me_b = champ_badge(me_art, icon_size, WHITE)
            opp_b = champ_badge(opp_art, icon_size, RED) if opp_art is not None else None
            f_vs = font(FONT_BOLD, 28)
            vs_w, vs_h = _line_size(draw, "VS", f_vs)
            gap = 22
            width = me_b.width + gap + vs_w + gap + (opp_b.width if opp_b is not None else 0)
            height = max(me_b.height, vs_h, opp_b.height if opp_b is not None else 0)
            prepared.append(
                {
                    "kind": "vs_icons",
                    "me": me_b,
                    "opp": opp_b,
                    "vs_font": f_vs,
                    "vs_w": vs_w,
                    "vs_h": vs_h,
                    "gap": gap,
                    "w": width,
                    "h": height,
                }
            )
        elif kind == "vs":
            champ, opp = item[1], item[2]
            f_name = fit_font(draw, f"{champ}    {opp}", FONT_BLACK, 54, max_w - 80, 28)
            f_vs = font(FONT_BOLD, max(22, int(f_name.size * 0.55)))
            parts = []
            if champ:
                parts.append((champ, f_name, WHITE))
            if opp:
                parts.append(("  VS  ", f_vs, MUTED))
                parts.append((opp, f_name, RED))
            width = sum(_line_size(draw, t, f)[0] for t, f, _c in parts)
            height = max((_line_size(draw, t, f)[1] for t, f, _c in parts), default=0)
            prepared.append({"kind": kind, "parts": parts, "w": width, "h": height})
        else:
            text = str(item[1] or "")
            if not text:
                continue
            if kind == "lp":
                fnt = fit_font(draw, text, FONT_BLACK, 68 if variant == "lp_only" else 52, max_w - 80, 28)
                fill = GOLD
                emblem = play_emblem
                tw_lp, th_lp = _line_size(draw, text, fnt)
                if variant == "lp_only":
                    emblem_h = 72
                elif variant == "road_lp":
                    emblem_h = even(max(40, th_lp + 2))
                else:
                    emblem_h = 56
            elif kind == "road":
                fnt = fit_font(draw, text, FONT_BLACK, 50, max_w - 48, 28)
                fill = GOLD
                emblem = None
                emblem_h = 0
            elif kind == "one" and me_art is not None:
                icon_size = 56
                me_b = champ_badge(me_art, icon_size, WHITE, radius=12)
                opp_b = champ_badge(opp_art, icon_size, RED, radius=12) if opp_art is not None else None
                fnt = fit_font(draw, overlay_lp_line(card), FONT_BLACK, 40, max_w - 220, 22)
                tw, th = _line_size(draw, overlay_lp_line(card), fnt)
                emblem = play_emblem
                emblem_h = 48
                ew = 0
                if emblem is not None:
                    ew = even(max(2, int(emblem.width * (emblem_h / max(emblem.height, 1)))))
                gap = 14
                width = me_b.width + gap
                if opp_b is not None:
                    width += opp_b.width + gap
                width += (ew + 10 if ew else 0) + tw
                height = max(me_b.height, th, emblem_h if ew else 0)
                prepared.append(
                    {
                        "kind": "one_icons",
                        "me": me_b,
                        "opp": opp_b,
                        "text": overlay_lp_line(card),
                        "font": fnt,
                        "fill": GOLD,
                        "emblem": emblem,
                        "emblem_w": ew,
                        "emblem_h": emblem_h if ew else 0,
                        "gap": gap,
                        "w": width,
                        "h": height,
                    }
                )
                continue
            else:
                fnt = fit_font(draw, text, FONT_BLACK, 42, max_w, 24)
                fill = WHITE
                emblem = play_emblem if kind == "one" else None
                emblem_h = 48 if emblem is not None else 0
            tw, th = _line_size(draw, text, fnt)
            ew = 0
            if emblem is not None:
                scale = emblem_h / max(emblem.height, 1)
                ew = even(max(2, int(emblem.width * scale)))
            width = tw + (ew + 14 if ew else 0)
            height = max(th, emblem_h if ew else 0)
            prepared.append(
                {
                    "kind": kind,
                    "text": text,
                    "font": fnt,
                    "fill": fill,
                    "emblem": emblem,
                    "emblem_w": ew,
                    "emblem_h": emblem_h if ew else 0,
                    "w": width,
                    "h": height,
                }
            )

    if not prepared:
        return canvas

    gap = 52 if spec["variant"] == "road_lp" and len(prepared) > 1 else (16 if len(prepared) > 1 else 0)
    block_h = sum(int(row["h"]) for row in prepared) + gap * (len(prepared) - 1)
    block_w = max(int(row["w"]) for row in prepared)
    y0 = int(cy - block_h / 2)
    pad_x, pad_y = (52, 40) if spec["variant"] == "road_lp" else (40, 26)
    pill = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0))
    ImageDraw.Draw(pill).rounded_rectangle(
        (
            (OUT_W - block_w) // 2 - pad_x,
            y0 - pad_y,
            (OUT_W + block_w) // 2 + pad_x,
            y0 + block_h + pad_y,
        ),
        radius=32,
        fill=(8, 10, 16, 150),
    )
    canvas.alpha_composite(pill)

    y = y0
    for row in prepared:
        mid = int(y + row["h"] / 2)
        if row["kind"] == "vs_icons":
            x = (OUT_W - int(row["w"])) // 2
            paste_center(canvas, row["me"], x + row["me"].width // 2, mid)
            x += row["me"].width + int(row["gap"])
            draw.text((x + 2, mid - int(row["vs_h"]) // 2 + 2), "VS", font=row["vs_font"], fill=(0, 0, 0))
            draw.text((x, mid - int(row["vs_h"]) // 2), "VS", font=row["vs_font"], fill=MUTED)
            x += int(row["vs_w"]) + int(row["gap"])
            if row["opp"] is not None:
                paste_center(canvas, row["opp"], x + row["opp"].width // 2, mid)
        elif row["kind"] == "one_icons":
            x = (OUT_W - int(row["w"])) // 2
            paste_center(canvas, row["me"], x + row["me"].width // 2, mid)
            x += row["me"].width + int(row["gap"])
            if row["opp"] is not None:
                paste_center(canvas, row["opp"], x + row["opp"].width // 2, mid)
                x += row["opp"].width + int(row["gap"])
            if row.get("emblem") is not None and row.get("emblem_w"):
                paste_center(
                    canvas,
                    row["emblem"],
                    x + int(row["emblem_w"]) // 2,
                    mid,
                    size=(int(row["emblem_w"]), int(row["emblem_h"])),
                )
                x += int(row["emblem_w"]) + 10
            tw, th = _line_size(draw, row["text"], row["font"])
            draw.text((x + 2, int(mid - th / 2) + 2), row["text"], font=row["font"], fill=(0, 0, 0))
            draw.text((x, int(mid - th / 2)), row["text"], font=row["font"], fill=row["fill"])
        elif row["kind"] == "vs":
            draw_centered_parts(draw, row["parts"], cy=mid)
        else:
            total = int(row["w"])
            x = (OUT_W - total) // 2
            if row.get("emblem") is not None and row.get("emblem_w"):
                paste_center(
                    canvas,
                    row["emblem"],
                    x + int(row["emblem_w"]) // 2,
                    mid,
                    size=(int(row["emblem_w"]), int(row["emblem_h"])),
                )
                x += int(row["emblem_w"]) + 12
            draw.text((x + 2, mid + 2), row["text"], font=row["font"], fill=(0, 0, 0), anchor="lm")
            draw.text((x, mid), row["text"], font=row["font"], fill=row["fill"], anchor="lm")
        y += int(row["h"]) + gap

    opacity = max(0.15, min(1.0, float(opacity)))
    if opacity < 0.995:
        alpha = canvas.getchannel("A").point(lambda p: int(p * opacity))
        canvas.putalpha(alpha)
    return canvas


def burn_overlay(
    source: Path,
    overlay_png: Path,
    output: Path,
    *,
    hold: float = 2.0,
    fade_in: float = 0.10,
    fade_out: float = 0.40,
    sting: Path | None = None,
) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    fade_out_at = max(0.0, float(hold) - float(fade_out))
    ov_t = max(0.2, float(hold) + 0.05)
    vfilt = (
        f"[1:v]format=rgba,fade=t=in:st=0:d={fade_in:.3f}:alpha=1,"
        f"fade=t=out:st={fade_out_at:.3f}:d={fade_out:.3f}:alpha=1[ov];"
        f"[0:v][ov]overlay=0:0:format=auto:eof_action=pass,format=yuv420p[v]"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-loop",
        "1",
        "-t",
        f"{ov_t:.3f}",
        "-i",
        str(overlay_png),
    ]
    if sting is not None and sting.is_file():
        delay_ms = 0
        cmd += ["-i", str(sting)]
        filt = (
            f"{vfilt};"
            f"[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[game];"
            f"[2:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"volume=14dB,adelay={delay_ms}|{delay_ms}:all=1[sting];"
            f"[game][sting]amix=inputs=2:duration=first:normalize=0:dropout_transition=0.15[a]"
        )
        cmd += ["-filter_complex", filt, "-map", "[v]", "-map", "[a]"]
    else:
        cmd += ["-filter_complex", vfilt, "-map", "[v]", "-map", "0:a?"]
    cmd += [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-shortest",
        "-movflags",
        "+faststart",
        str(output),
    ]
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"ffmpeg overlay failed\n{detail}")
    return output


def write_overlay_variants(
    card: dict[str, Any],
    out_dir: Path,
    *,
    source: Path,
    stem: str,
    variants: list[str],
    hold: float,
    opacity: float,
    cy: int,
    still: Path | None = None,
    sting: Path | None = None,
) -> dict[str, Any]:
    from PIL import Image

    out_dir.mkdir(parents=True, exist_ok=True)
    frame = Image.open(still).convert("RGBA") if still is not None and still.is_file() else None
    outputs: dict[str, Any] = {}
    for variant in variants:
        name = f"{stem}_overlay_{variant}"
        png = out_dir / f"{name}.png"
        mp4 = out_dir / f"{name}.mp4"
        ov = render_overlay_png(card, variant, cy=cy, opacity=opacity)
        ov.save(png)
        burn_overlay(source, png, mp4, hold=hold, sting=sting)
        item = {"png": str(png), "mp4": str(mp4)}
        if frame is not None:
            still_path = out_dir / f"{name}_still.jpg"
            Image.alpha_composite(frame, ov).convert("RGB").save(still_path, quality=92)
            item["still"] = str(still_path)
        outputs[variant] = item
    return outputs


def card_from_meta(
    meta: dict[str, Any],
    *,
    meta_path: Path | None = None,
    identity_tier: str = "CHALLENGER",
    handle: str = "lolAmbrosek",
    cta: str = "LIVE · twitch.tv/lolAmbrosek",
    challenger_cutoff: int | None = None,
    cutoff_platform: str = "na1",
    estimate_lp: bool = True,
) -> dict[str, Any]:
    """Lobby sidecar → rank-card dict, including live Challenger cutoff."""
    card = resolve_card(
        meta,
        identity_tier=identity_tier,
        tagline="",
        handle=handle,
        cta=cta,
        start_lp=None,
        end_lp=None,
        lp_delta=None,
        estimate_lp=estimate_lp,
        date_label=format_day_label("", from_path=meta_path),
    )
    champ = str(card.get("champion") or "UNKNOWN")
    card["bgSkin"] = resolve_bg_skin(str(card.get("championKey") or champ))
    card.setdefault("overlaySub", f"RANK 1 {champ} NA")
    card["streamers"] = detect_streamers(meta)
    card["cutoffPlatform"] = cutoff_platform
    if challenger_cutoff is not None:
        card["challengerCutoffLp"] = int(challenger_cutoff)
    elif card.get("challengerCutoffLp") is None:
        live = fetch_challenger_cutoff(platform=cutoff_platform)
        if live is not None:
            card["challengerCutoffLp"] = live
            print(f"[cutoff] live {cutoff_platform} Challenger {live:,} LP", flush=True)
        else:
            card["challengerCutoffLp"] = DEFAULT_CHALLENGER_CUTOFF
            print(
                f"[cutoff] using fallback {DEFAULT_CHALLENGER_CUTOFF:,} LP "
                "(pass --challenger-cutoff or set RIOT_API_KEY)",
                flush=True,
            )
    return card


def _probe_av(path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,r_frame_rate",
            "-of",
            "json",
            str(path),
        ],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or "ffprobe failed")
    data = json.loads(proc.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    rate = str(stream.get("r_frame_rate") or "30/1")
    fps = 30.0
    if "/" in rate:
        num, den = rate.split("/", 1)
        try:
            fps = float(num) / max(float(den), 1.0)
        except ValueError:
            fps = 30.0
    audio = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(path),
        ],
        text=True,
        capture_output=True,
    )
    return {
        "width": int(stream.get("width") or OUT_W),
        "height": int(stream.get("height") or OUT_H),
        "duration": float((data.get("format") or {}).get("duration") or 0.0),
        "fps": max(1.0, min(fps, 60.0)),
        "has_audio": bool((audio.stdout or "").strip()),
    }


def concat_clips(
    parts: list[Path],
    output: Path,
    *,
    crf: int = 20,
    preset: str = "veryfast",
) -> Path:
    """Re-encode concat so the end card matches the portrait stream."""
    output.parent.mkdir(parents=True, exist_ok=True)
    if len(parts) == 1:
        if parts[0].resolve() != output.resolve():
            shutil.copy2(parts[0], output)
        return output
    info = _probe_av(parts[0])
    w = even(int(info["width"]))
    h = even(int(info["height"]))
    rate = float(info["fps"])
    cmd: list[str] = ["ffmpeg", "-y"]
    for clip in parts:
        cmd += ["-i", str(clip)]
    silent_idx: int | None = None
    if not info["has_audio"]:
        silent_idx = len(parts)
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
    filter_parts: list[str] = []
    for i, clip in enumerate(parts):
        clip_info = info if i == 0 else _probe_av(clip)
        filter_parts.append(
            f"[{i}:v]setpts=PTS-STARTPTS,"
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x0A0C12,"
            f"fps={rate:.3f},setsar=1,format=yuv420p[v{i}];"
        )
        if clip_info["has_audio"]:
            filter_parts.append(
                f"[{i}:a]asetpts=PTS-STARTPTS,"
                f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a{i}];"
            )
        elif silent_idx is not None and i == 0:
            filter_parts.append(
                f"[{silent_idx}:a]atrim=0:{max(0.05, float(clip_info['duration'])):.3f},"
                f"asetpts=PTS-STARTPTS,"
                f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a{i}];"
            )
        else:
            dur = max(0.05, float(clip_info["duration"]))
            filter_parts.append(
                f"anullsrc=r=48000:cl=stereo:d={dur:.3f}[a{i}];"
            )
    concat_in = "".join(f"[v{i}][a{i}]" for i in range(len(parts)))
    cmd += [
        "-filter_complex",
        "".join(filter_parts) + f"{concat_in}concat=n={len(parts)}:v=1:a=1[v][a]",
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        str(output),
    ]
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"ffmpeg concat failed\n{detail}")
    return output


def overlay_then_concat(
    source: Path,
    overlay_png: Path,
    outro: Path,
    output: Path,
    *,
    hold: float = 2.0,
    fade_in: float = 0.10,
    fade_out: float = 0.40,
    sting: Path | None = None,
    crf: int = 20,
    preset: str = "veryfast",
) -> Path:
    """One encode: fade overlay onto gameplay, then append the end card."""
    output.parent.mkdir(parents=True, exist_ok=True)
    info = _probe_av(source)
    w = even(int(info["width"]))
    h = even(int(info["height"]))
    rate = float(info["fps"])
    fade_out_at = max(0.0, float(hold) - float(fade_out))
    ov_t = max(0.2, float(hold) + 0.05)
    outro_info = _probe_av(outro)
    out_t = max(0.2, float(info["duration"]) + float(outro_info["duration"]) + 0.25)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-loop",
        "1",
        "-t",
        f"{ov_t:.3f}",
        "-i",
        str(overlay_png),
        "-i",
        str(outro),
    ]
    sting_ok = sting is not None and sting.is_file()
    if sting_ok:
        cmd += ["-i", str(sting)]
    if info["has_audio"]:
        game_src = (
            "[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[game];"
        )
    else:
        game_src = (
            f"anullsrc=r=48000:cl=stereo:d={max(0.05, float(info['duration'])):.3f}[game];"
        )
    if sting_ok:
        game_a = (
            f"{game_src}"
            f"[3:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"volume=14dB[sting];"
            f"[game][sting]amix=inputs=2:duration=first:normalize=0:dropout_transition=0.15[a0];"
        )
    else:
        game_a = game_src.replace("[game];", "[a0];")
    filt = (
        f"[1:v]format=rgba,fade=t=in:st=0:d={fade_in:.3f}:alpha=1,"
        f"fade=t=out:st={fade_out_at:.3f}:d={fade_out:.3f}:alpha=1[ov];"
        f"[0:v][ov]overlay=0:0:format=auto:eof_action=pass,setpts=PTS-STARTPTS,"
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x0A0C12,"
        f"fps={rate:.3f},setsar=1,format=yuv420p[v0];"
        f"{game_a}"
        f"[2:v]setpts=PTS-STARTPTS,"
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x0A0C12,"
        f"fps={rate:.3f},setsar=1,format=yuv420p[v1];"
        f"[2:a]asetpts=PTS-STARTPTS,"
        f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a1];"
        f"[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]"
    )
    cmd += [
        "-filter_complex",
        filt,
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-t",
        f"{out_t:.3f}",
        "-movflags",
        "+faststart",
        str(output),
    ]
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"ffmpeg overlay+outro failed\n{detail}")
    return output


def wrap_portrait(
    portrait: Path,
    meta_path: Path,
    *,
    output: Path | None = None,
    work_dir: Path | None = None,
    intro: bool = True,
    outro: bool = True,
    overlay_hold: float = 2.0,
    end_seconds: float = 2.5,
    crf: int = 20,
    preset: str = "veryfast",
    stem: str | None = None,
    challenger_cutoff: int | None = None,
    cutoff_platform: str = "na1",
) -> dict[str, Any]:
    """Burn the road_lp overlay onto a portrait and/or append the end card."""
    from PIL import Image

    if not portrait.is_file():
        raise FileNotFoundError(f"missing portrait {portrait}")
    if not meta_path.is_file():
        raise FileNotFoundError(f"missing lobby meta {meta_path}")
    if not intro and not outro:
        return {"output": str(portrait), "intro": False, "outro": False}

    final = output or portrait
    work = work_dir or final.parent / "rank_cards"
    work.mkdir(parents=True, exist_ok=True)
    name = stem or portrait.stem.replace("_portrait", "") or "rank"
    card = card_from_meta(
        load_json(meta_path),
        meta_path=meta_path,
        challenger_cutoff=challenger_cutoff,
        cutoff_platform=cutoff_platform,
    )

    overlay_png: Path | None = None
    overlay_sting = DEFAULT_OVERLAY_STING if DEFAULT_OVERLAY_STING.is_file() else None
    if intro:
        overlay_png = work / f"{name}_overlay_road_lp.png"
        render_road_progress_overlay(card).save(overlay_png)
        print(f"[portrait] overlay intro {overlay_png.name} ({overlay_hold:.1f}s)", flush=True)

    end_mp4: Path | None = None
    if outro:
        end_face_path = DEFAULT_END_FACE if DEFAULT_END_FACE.is_file() else DEFAULT_FACE
        if not end_face_path.is_file():
            print("[portrait] skip outro: missing end-card face still", flush=True)
        else:
            end_mp4 = work / f"{name}_end.mp4"
            end_face = Image.open(end_face_path).convert("RGBA")
            end_sting = default_end_sting(card)
            sting_note = f" sting={end_sting.name}" if end_sting else ""
            print(
                f"[portrait] end outro {end_mp4.name} ({end_seconds:.1f}s)"
                f"{' loss' if negative_lp(card) else ' win'}{sting_note}",
                flush=True,
            )
            write_clip(
                card,
                end_face,
                end_mp4,
                kind="end",
                seconds=end_seconds,
                sting=end_sting,
            )

    if overlay_png is None and end_mp4 is None:
        return {
            "output": str(portrait),
            "intro": False,
            "outro": False,
            "overlayPng": None,
            "endMp4": None,
            "card": card,
        }

    tmp_final = final.with_name(f"{final.stem}._wrap.mp4")
    try:
        if overlay_png is not None and end_mp4 is not None:
            overlay_then_concat(
                portrait,
                overlay_png,
                end_mp4,
                tmp_final,
                hold=overlay_hold,
                sting=overlay_sting,
                crf=crf,
                preset=preset,
            )
        elif overlay_png is not None:
            burn_overlay(
                portrait,
                overlay_png,
                tmp_final,
                hold=overlay_hold,
                sting=overlay_sting,
            )
        else:
            concat_clips([portrait, end_mp4], tmp_final, crf=crf, preset=preset)
        tmp_final.replace(final)
    finally:
        tmp_final.unlink(missing_ok=True)

    return {
        "output": str(final),
        "intro": overlay_png is not None,
        "outro": end_mp4 is not None,
        "overlayPng": str(overlay_png) if overlay_png else None,
        "endMp4": str(end_mp4) if end_mp4 else None,
        "card": card,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Render 9:16 start/end rank cards.")
    p.add_argument("--meta", type=Path, required=True, help="Lobby sidecar JSON")
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--face", type=Path, default=DEFAULT_FACE)
    p.add_argument(
        "--end-face",
        type=Path,
        default=DEFAULT_END_FACE,
        help="Centered still for the end card (default: assets/brand/heart_hands.png)",
    )
    p.add_argument(
        "--sting",
        type=Path,
        default=None,
        help="WAV mixed onto the end card. Default: magic sting on wins, "
        "negative_beeps on losses",
    )
    p.add_argument(
        "--loss-sting",
        type=Path,
        default=None,
        help="WAV for negative-LP outros (default: negative_beeps in suggested/)",
    )
    p.add_argument("--identity-tier", default="CHALLENGER")
    p.add_argument(
        "--road-line",
        default="ROAD BACK UP TO CHALLENGER",
        help="Start-card destination line (split on ' TO ')",
    )
    p.add_argument("--tagline", default="")
    p.add_argument("--peak-lp", type=int, default=None, help="Optional peak LP line under the identity rank")
    p.add_argument(
        "--date",
        default="",
        help="Start-card record stamp, e.g. 'AUG 15/2026'. Default: meta file day",
    )
    p.add_argument("--handle", default="lolAmbrosek")
    p.add_argument("--cta", default="LIVE · twitch.tv/lolAmbrosek")
    p.add_argument("--start-lp", type=int, default=None)
    p.add_argument("--end-lp", type=int, default=None)
    p.add_argument("--lp-delta", type=int, default=None)
    p.add_argument(
        "--estimate-lp",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="If only one LP is known, back out start/end from a typical ranked swing",
    )
    p.add_argument("--start-seconds", type=float, default=1.8)
    p.add_argument("--end-seconds", type=float, default=2.5)
    p.add_argument(
        "--only",
        choices=("both", "start", "end", "overlay"),
        default="both",
        help="Render start, end, overlay, or both cards (default: both)",
    )
    p.add_argument(
        "--overlay-on",
        type=Path,
        default=None,
        help="Gameplay mp4 to burn opening overlay variants onto",
    )
    p.add_argument(
        "--overlay-hold",
        type=float,
        default=2.0,
        help="Seconds the overlay stays up before fading (default: 2)",
    )
    p.add_argument(
        "--overlay-opacity",
        type=float,
        default=0.90,
        help="Overall overlay opacity 0-1 (default: 0.90)",
    )
    p.add_argument(
        "--overlay-y",
        type=int,
        default=1180,
        help="Vertical center of the overlay on 1080x1920 (default: 1180)",
    )
    p.add_argument(
        "--overlay-variants",
        default=",".join(OVERLAY_VARIANTS),
        help="Comma list: vs_lp,one_line,lp_only,road_lp",
    )
    p.add_argument(
        "--overlay-still",
        type=Path,
        default=None,
        help="Optional gameplay JPEG to composite overlay stills onto",
    )
    p.add_argument(
        "--overlay-sting",
        type=Path,
        default=DEFAULT_OVERLAY_STING,
        help="WAV mixed on as the overlay fades out (default: sparkle sting)",
    )
    p.add_argument(
        "--bg-skin",
        type=int,
        default=None,
        help="DDragon skin number for the card backdrop (LeBlanc default: 45 Bewitching)",
    )
    p.add_argument("--stem", default="rank")
    p.add_argument(
        "--challenger-cutoff",
        type=int,
        default=None,
        help="Challenger LP cutoff for the progress bar. Default: live NA ladder today",
    )
    p.add_argument(
        "--cutoff-platform",
        default="na1",
        help="Riot platform for the live Challenger cutoff (default: na1)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    from PIL import Image

    args = build_parser().parse_args(argv)
    meta_path = args.meta.resolve()
    if not meta_path.is_file():
        print(f"error: missing meta {meta_path}", file=sys.stderr)
        return 1
    want = str(args.only)
    overlay_src = args.overlay_on.resolve() if args.overlay_on else None
    if overlay_src is not None and not overlay_src.is_file():
        print(f"error: missing overlay source {overlay_src}", file=sys.stderr)
        return 1
    if want == "overlay" and overlay_src is None:
        print("error: --only overlay requires --overlay-on", file=sys.stderr)
        return 1
    face_path = args.face.resolve()
    if want in {"both", "start"} and not face_path.is_file():
        print(f"error: missing face still {face_path}", file=sys.stderr)
        return 1
    end_face_path = args.end_face.resolve() if args.end_face else face_path
    if want in {"both", "end"} and not end_face_path.is_file():
        end_face_path = face_path
        if not end_face_path.is_file():
            print(f"error: missing end face still {args.end_face}", file=sys.stderr)
            return 1
    meta = load_json(meta_path)
    card = resolve_card(
        meta,
        identity_tier=str(args.identity_tier),
        tagline=str(args.tagline),
        handle=str(args.handle),
        cta=str(args.cta),
        start_lp=args.start_lp,
        end_lp=args.end_lp,
        lp_delta=args.lp_delta,
        estimate_lp=bool(args.estimate_lp),
        peak_lp=args.peak_lp,
        date_label=format_day_label(str(args.date or ""), from_path=meta_path),
        road_line=str(args.road_line),
    )
    card["bgSkin"] = resolve_bg_skin(str(card.get("championKey") or card["champion"]), args.bg_skin)
    card["streamers"] = detect_streamers(meta)
    card["cutoffPlatform"] = str(args.cutoff_platform)
    win_sting = args.sting.resolve() if args.sting else None
    if win_sting is not None and not win_sting.is_file():
        win_sting = None
    loss_sting = args.loss_sting.resolve() if args.loss_sting else None
    if loss_sting is not None and not loss_sting.is_file():
        loss_sting = None
    if args.sting is not None:
        sting_path = win_sting
    else:
        sting_path = default_end_sting(card, loss_sting=loss_sting)
    if args.challenger_cutoff is not None:
        card["challengerCutoffLp"] = int(args.challenger_cutoff)
    elif card.get("challengerCutoffLp") is None:
        live = fetch_challenger_cutoff(platform=str(args.cutoff_platform))
        if live is not None:
            card["challengerCutoffLp"] = live
            print(f"[cutoff] live {args.cutoff_platform} Challenger {live:,} LP", flush=True)
        else:
            card["challengerCutoffLp"] = DEFAULT_CHALLENGER_CUTOFF
            print(
                f"[cutoff] using fallback {DEFAULT_CHALLENGER_CUTOFF:,} LP "
                "(pass --challenger-cutoff or set RIOT_API_KEY)",
                flush=True,
            )
    face_im = Image.open(face_path).convert("RGBA") if want in {"both", "start"} and face_path.is_file() else None
    end_face_im = (
        Image.open(end_face_path).convert("RGBA")
        if want in {"both", "end"} and end_face_path.is_file()
        else None
    )
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = str(args.stem)

    start_png = out_dir / f"{stem}_start.png"
    end_png = out_dir / f"{stem}_end.png"
    start_mp4 = out_dir / f"{stem}_start.mp4"
    end_mp4 = out_dir / f"{stem}_end.mp4"
    outputs: dict[str, Any] = {}
    if want in {"both", "start"}:
        assert face_im is not None
        render_card(card, face_im, kind="start").save(start_png, quality=95)
        write_clip(
            card,
            face_im,
            start_mp4,
            kind="start",
            seconds=float(args.start_seconds),
        )
        outputs["startPng"] = str(start_png)
        outputs["startMp4"] = str(start_mp4)
    if want in {"both", "end"}:
        shown, delta, punch, _prog = ticker_values(card, 99.0, fps=30.0)
        render_card(
            card,
            end_face_im,
            kind="end",
            shown_lp=shown,
            shown_delta=delta,
            delta_scale=punch,
        ).save(end_png, quality=95)
        write_clip(
            card,
            end_face_im,
            end_mp4,
            kind="end",
            seconds=float(args.end_seconds),
            sting=sting_path,
        )
        outputs["endPng"] = str(end_png)
        outputs["endMp4"] = str(end_mp4)
    if overlay_src is not None or want == "overlay":
        names = [v.strip() for v in str(args.overlay_variants).split(",") if v.strip()]
        unknown = [v for v in names if v not in OVERLAY_VARIANTS]
        if unknown:
            print(f"error: unknown overlay variants {unknown}", file=sys.stderr)
            return 1
        if overlay_src is None:
            print("error: --overlay-on is required for overlay output", file=sys.stderr)
            return 1
        overlay_sting = args.overlay_sting.resolve() if args.overlay_sting else None
        if overlay_sting is not None and not overlay_sting.is_file():
            overlay_sting = None
        outputs["overlays"] = write_overlay_variants(
            card,
            out_dir,
            source=overlay_src,
            stem=stem,
            variants=names,
            hold=float(args.overlay_hold),
            opacity=float(args.overlay_opacity),
            cy=int(args.overlay_y),
            still=args.overlay_still.resolve() if args.overlay_still else None,
            sting=overlay_sting,
        )
    report = {
        "status": "ok",
        "card": card,
        "face": str(face_path),
        "endFace": str(end_face_path),
        "sting": str(sting_path) if sting_path else None,
        "outputs": outputs,
    }
    (out_dir / f"{stem}_meta.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
