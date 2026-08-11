#!/usr/bin/env python3
"""Generate a LoL client-style loading screen lobby card for a match."""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent / "lol-indexer"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from env_loader import load_dotenv
from riot_api import RiotAPI, RiotAPIError

CDRAGON = "https://raw.communitydragon.org/latest/plugins/rcp-fe-lol-static-assets/global/default"
DDRAGON_LOADING = "https://ddragon.leagueoflegends.com/cdn/img/champion/loading"

ROLE_ORDER = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
ROLE_LABELS = {
    "TOP": "top",
    "JUNGLE": "jungle",
    "MIDDLE": "mid",
    "BOTTOM": "bot",
    "UTILITY": "support",
}

TIER_COLORS: dict[str, tuple[int, int, int]] = {
    "IRON": (110, 110, 120),
    "BRONZE": (160, 100, 60),
    "SILVER": (170, 180, 195),
    "GOLD": (220, 175, 70),
    "PLATINUM": (90, 190, 175),
    "EMERALD": (50, 170, 110),
    "DIAMOND": (110, 170, 230),
    "MASTER": (170, 90, 200),
    "GRANDMASTER": (200, 60, 70),
    "CHALLENGER": (90, 180, 230),
    "UNRANKED": (120, 125, 140),
}

CHAMP_ALIASES = {
    "FiddleSticks": "Fiddlesticks",
    "Wukong": "MonkeyKing",
    "Renata Glasc": "Renata",
    "Nunu & Willump": "Nunu",
}


def http_bytes(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "poststream-lobby-card/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def platform_from_match_id(match_id: str) -> str:
    prefix = match_id.split("_", 1)[0].lower()
    return prefix or "na1"


def champ_key(champ: str) -> str:
    return CHAMP_ALIASES.get(champ, champ.replace(" ", "").replace("'", ""))


def parse_solo_entry(entries: list[dict[str, Any]]) -> dict[str, Any]:
    for e in entries:
        if e.get("queueType") != "RANKED_SOLO_5x5":
            continue
        tier = str(e.get("tier") or "UNRANKED").upper()
        rank = str(e.get("rank") or "")
        lp = e.get("leaguePoints")
        if tier in {"MASTER", "GRANDMASTER", "CHALLENGER"}:
            label = f"{tier.title()} {lp} LP" if lp is not None else tier.title()
        elif tier == "UNRANKED":
            label = "Unranked"
        else:
            label = f"{tier.title()} {rank}"
            if lp is not None:
                label = f"{label} ({lp} LP)"
        return {"tier": tier, "rank": rank, "lp": lp, "label": label}
    return {"tier": "UNRANKED", "rank": "", "lp": None, "label": "Unranked"}


def build_lobby_rows(match: dict[str, Any], api: RiotAPI, platform: str) -> list[dict[str, Any]]:
    info = match.get("info") or {}
    rows: list[dict[str, Any]] = []
    for p in info.get("participants") or []:
        puuid = p.get("puuid")
        entries: list[dict[str, Any]] = []
        if puuid:
            try:
                entries = api.get_league_entries_by_puuid(str(puuid), platform=platform)
            except RiotAPIError:
                entries = []
        rank = parse_solo_entry(entries)
        rows.append(
            {
                "teamId": int(p.get("teamId") or 0),
                "champion": str(p.get("championName") or "?"),
                "name": str(p.get("riotIdGameName") or p.get("summonerName") or "?"),
                "tag": str(p.get("riotIdTagline") or ""),
                "position": str(p.get("teamPosition") or p.get("individualPosition") or ""),
                "tier": rank["tier"],
                "rank": rank["rank"],
                "lp": rank["lp"],
                "rankLabel": rank["label"],
                "win": bool(p.get("win")),
            }
        )
    return rows


def sort_by_role(players: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(p: dict[str, Any]) -> tuple[int, str]:
        pos = str(p.get("position") or "").upper()
        try:
            return (ROLE_ORDER.index(pos), p.get("name") or "")
        except ValueError:
            return (99, p.get("name") or "")

    ordered = sorted(players, key=key)
    while len(ordered) < 5:
        ordered.append(
            {
                "champion": "?",
                "name": "—",
                "tag": "",
                "position": "",
                "tier": "UNRANKED",
                "rankLabel": "",
            }
        )
    return ordered[:5]


_asset_cache: dict[str, bytes | None] = {}


def cached_bytes(url: str) -> bytes | None:
    if url in _asset_cache:
        return _asset_cache[url]
    try:
        data = http_bytes(url)
    except Exception:
        data = None
    _asset_cache[url] = data
    return data


def open_rgba(data: bytes):
    from PIL import Image

    return Image.open(io.BytesIO(data)).convert("RGBA")


def loading_splash(champ: str):
    key = champ_key(champ)
    data = cached_bytes(f"{DDRAGON_LOADING}/{key}_0.jpg")
    if not data:
        return None
    return open_rgba(data)


def tier_emblem(tier: str):
    from PIL import Image

    t = tier.lower()
    if t == "unranked":
        data = cached_bytes(f"{CDRAGON}/ranked-mini-crests/unranked.png")
        return open_rgba(data) if data else None
    data = cached_bytes(f"{CDRAGON}/ranked-emblem/emblem-{t}.png")
    if not data:
        return None
    im = open_rgba(data)
    bbox = im.getbbox()
    if bbox:
        im = im.crop(bbox)
    return im


def tier_wings(tier: str):
    t = tier.lower()
    if t == "unranked":
        return None
    data = cached_bytes(f"{CDRAGON}/ranked-emblem/wings/wings_{t}.png")
    if not data:
        return None
    im = open_rgba(data)
    bbox = im.getbbox()
    if bbox:
        im = im.crop(bbox)
    return im


def paste_center(base, overlay, cx: int, cy: int, size: tuple[int, int] | None = None) -> None:
    from PIL import Image

    ov = overlay
    if size is not None:
        ov = overlay.resize(size, Image.Resampling.LANCZOS)
    x = int(cx - ov.width / 2)
    y = int(cy - ov.height / 2)
    base.alpha_composite(ov, (x, y))


def draw_tier_border(draw, box: tuple[int, int, int, int], tier: str) -> None:
    """Ornate rank frame around a loading portrait (no team-color border)."""
    x0, y0, x1, y1 = box
    color = TIER_COLORS.get(tier.upper(), TIER_COLORS["UNRANKED"])
    # Rank metal frame
    draw.rectangle([x0 - 1, y0 - 1, x1 + 1, y1 + 1], outline=color + (255,), width=5)
    draw.rectangle([x0 + 4, y0 + 4, x1 - 4, y1 - 4], outline=(255, 255, 255, 70), width=1)
    # Corner accents
    c = 18
    for ax, ay, dx, dy in [
        (x0, y0, 1, 1),
        (x1, y0, -1, 1),
        (x0, y1, 1, -1),
        (x1, y1, -1, -1),
    ]:
        draw.line([(ax, ay), (ax + dx * c, ay)], fill=color + (255,), width=3)
        draw.line([(ax, ay), (ax, ay + dy * c)], fill=color + (255,), width=3)


def fit_text(
    draw,
    text: str,
    *,
    max_width: int,
    font_path: str,
    start_size: int,
    min_size: int = 11,
):
    """Shrink font (then ellipsize) until text fits max_width. Returns (text, font)."""
    from PIL import ImageFont

    for size in range(start_size, min_size - 1, -1):
        try:
            font = ImageFont.truetype(font_path, size)
        except OSError:
            font = ImageFont.load_default()
            break
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= max_width:
            return text, font

    try:
        font = ImageFont.truetype(font_path, min_size)
    except OSError:
        font = ImageFont.load_default()

    # Ellipsize at min size
    out = text
    while out and draw.textbbox((0, 0), out + "…", font=font)[2] > max_width:
        out = out[:-1]
    if out != text:
        out = (out.rstrip() + "…") if out else "…"
    return out, font


def display_name(player: dict[str, Any], max_width: int, draw, font_path: str):
    """
    Prefer name#tag. If too long even at small size, drop tag then ellipsize game name.
    """
    game = str(player.get("name") or "?")
    tag = str(player.get("tag") or "")
    full = f"{game}#{tag}" if tag else game
    text, font = fit_text(draw, full, max_width=max_width, font_path=font_path, start_size=18)
    # If we had to heavily shrink/ellipsize a long tagline name, try game name alone
    if tag and ("…" in text or len(full) > 22):
        game_only, font2 = fit_text(
            draw, game, max_width=max_width, font_path=font_path, start_size=18
        )
        # Prefer readable game name over mangled full riot id when both truncate
        if "…" not in game_only or len(game_only) >= len(text) - 2:
            return game_only, font2
    return text, font


def role_label(position: str) -> str:
    return ROLE_LABELS.get(str(position or "").upper(), str(position or ""))


def render_lobby_card(
    rows: list[dict[str, Any]],
    *,
    output: Path,
    highlight_name: str | None = None,
    width: int = 1920,
    height: int = 1080,
) -> Path:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter

    canvas = Image.new("RGBA", (width, height), (8, 10, 18, 255))
    draw = ImageDraw.Draw(canvas)

    # Atmospheric background — blue wash on top half, red on bottom
    for y in range(height):
        t = y / height
        r = int(10 + 22 * t)
        g = int(12 + 10 * t)
        b = int(24 + 20 * (1 - t))
        draw.line([(0, y), (width, y)], fill=(r, g, b, 255))

    blue_wash = Image.new("RGBA", (width, height // 2), (40, 90, 180, 30))
    red_wash = Image.new("RGBA", (width, height - height // 2), (160, 40, 50, 30))
    canvas.alpha_composite(blue_wash, (0, 0))
    canvas.alpha_composite(red_wash, (0, height // 2))

    font_path_bold = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
    font_path = "/System/Library/Fonts/Supplemental/Arial.ttf"
    try:
        font_vs = ImageFont.truetype(font_path_bold, 42)
        font_role = ImageFont.truetype(font_path_bold, 15)
    except OSError:
        font_vs = ImageFont.load_default()
        font_role = font_vs

    blue = sort_by_role([r for r in rows if r["teamId"] == 100])
    red = sort_by_role([r for r in rows if r["teamId"] == 200])

    hl = (highlight_name or "").strip().lower()

    def is_me(player: dict[str, Any]) -> bool:
        if not hl:
            return False
        name = str(player.get("name") or "").lower()
        tag = str(player.get("tag") or "").lower()
        full = f"{name}#{tag}" if tag else name
        if hl == full or hl == name:
            return True
        # RIOT_ID may be "twtv lolAmbrosek#twtv"
        if "#" in hl:
            g, t = hl.split("#", 1)
            return name == g and (not t or tag == t)
        return name == hl or hl.endswith(name)

    # Portrait geometry — blue row on top, red row on bottom
    card_w, card_h = 168, 300
    gap = 28
    row_width = 5 * card_w + 4 * gap
    x0 = (width - row_width) // 2
    name_block = 54
    role_pad = 28  # space above card for role label
    vs_band = 120  # padding between blue names and red role labels
    blue_y = 40 + role_pad
    red_y = blue_y + card_h + name_block + vs_band
    if red_y + card_h + name_block > height - 20:
        overflow = (red_y + card_h + name_block) - (height - 20)
        card_h = max(240, card_h - overflow)
        red_y = blue_y + card_h + name_block + vs_band

    # VS sits in the upper part of the band so more padding sits above the bottom row
    vs_y = blue_y + card_h + name_block + 18
    vb = draw.textbbox((0, 0), "VS", font=font_vs)
    draw.text(((width - (vb[2] - vb[0])) // 2, vs_y), "VS", fill=(230, 230, 240, 230), font=font_vs)
    mid_y = vs_y + (vb[3] - vb[1]) // 2
    draw.line([(x0, mid_y), (width // 2 - 50, mid_y)], fill=(255, 255, 255, 40), width=2)
    draw.line([(width // 2 + 50, mid_y), (x0 + row_width, mid_y)], fill=(255, 255, 255, 40), width=2)

    def render_player(
        player: dict[str, Any],
        x: int,
        y: int,
        accent: tuple[int, int, int],
        *,
        mine: bool,
    ) -> None:
        cw, ch = card_w, card_h
        ox, oy = x, y

        tier = str(player.get("tier") or "UNRANKED").upper()
        splash = loading_splash(str(player.get("champion") or "?"))
        card = Image.new("RGBA", (cw, ch), (20, 22, 30, 255))
        if splash is not None:
            sw, sh = splash.size
            scale = max(cw / sw, ch / sh)
            nw, nh = int(sw * scale), int(sh * scale)
            splash = splash.resize((nw, nh), Image.Resampling.LANCZOS)
            sx = (nw - cw) // 2
            sy = (nh - ch) // 2
            splash = splash.crop((sx, sy, sx + cw, sy + ch))
            card.alpha_composite(splash)

        vig = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        vd = ImageDraw.Draw(vig)
        for i in range(90):
            a = int(180 * (i / 90) ** 1.4)
            yy = ch - 90 + i
            vd.line([(0, yy), (cw, yy)], fill=(0, 0, 0, a))
        card.alpha_composite(vig)

        wings = tier_wings(tier)
        emblem = tier_emblem(tier)
        badge_cx, badge_cy = cw // 2, ch - 52
        if wings is not None:
            ww = int(cw * 1.35)
            wh = int(ww * wings.height / max(1, wings.width))
            paste_center(card, wings, badge_cx, badge_cy + 4, (ww, wh))
        if emblem is not None:
            ew = 78
            eh = int(ew * emblem.height / max(1, emblem.width))
            paste_center(card, emblem, badge_cx, badge_cy, (ew, eh))

        shadow = Image.new("RGBA", (cw + 16, ch + 16), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        sd.rectangle([6, 8, cw + 6, ch + 8], fill=(0, 0, 0, 110))
        shadow = shadow.filter(ImageFilter.GaussianBlur(6))
        canvas.alpha_composite(shadow, (ox - 6, oy - 4))
        canvas.alpha_composite(card, (ox, oy))

        frame = ImageDraw.Draw(canvas)
        draw_tier_border(frame, (ox, oy, ox + cw, oy + ch), tier)

        # Role above card
        pos = role_label(str(player.get("position") or ""))
        if pos:
            pb = frame.textbbox((0, 0), pos, font=font_role)
            pw = pb[2] - pb[0]
            frame.text((ox + (cw - pw) // 2, oy - 22), pos, fill=accent + (220,), font=font_role)

        # Name under card — highlight mine with gold text only
        name, name_font = display_name(player, max_width=cw - 8, draw=frame, font_path=font_path_bold)
        nb = frame.textbbox((0, 0), name, font=name_font)
        nw = nb[2] - nb[0]
        name_fill = (255, 220, 110, 255) if mine else (235, 235, 240, 255)
        frame.text((ox + (cw - nw) // 2, oy + ch + 10), name, fill=name_fill, font=name_font)

        rank_label = str(player.get("rankLabel") or "")
        if rank_label:
            rank_text, rank_font = fit_text(
                frame,
                rank_label,
                max_width=cw - 8,
                font_path=font_path,
                start_size=15,
                min_size=11,
            )
            rb = frame.textbbox((0, 0), rank_text, font=rank_font)
            rw = rb[2] - rb[0]
            color = TIER_COLORS.get(tier, TIER_COLORS["UNRANKED"])
            frame.text(
                (ox + (cw - rw) // 2, oy + ch + 32),
                rank_text,
                fill=color + (255,),
                font=rank_font,
            )

    for i, player in enumerate(blue):
        render_player(
            player,
            x0 + i * (card_w + gap),
            blue_y,
            (70, 140, 255),
            mine=is_me(player),
        )
    for i, player in enumerate(red):
        render_player(
            player,
            x0 + i * (card_w + gap),
            red_y,
            (230, 80, 80),
            mine=is_me(player),
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(output, format="PNG", optimize=True)
    return output


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Render a client-style lobby loading card PNG.")
    p.add_argument("--match-id", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--platform", default=None, help="Override platform (default: from match id)")
    p.add_argument(
        "--highlight",
        default=None,
        help="Riot ID to highlight (default: RIOT_ID env)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    api_key = os.environ.get("RIOT_API_KEY", "").strip()
    if not api_key:
        print("error: RIOT_API_KEY missing", file=sys.stderr)
        return 1
    region = os.environ.get("RIOT_REGION", "americas")
    api = RiotAPI(api_key, region=region)
    match = api.get_match(args.match_id)
    platform = args.platform or platform_from_match_id(args.match_id)
    rows = build_lobby_rows(match, api, platform)

    highlight = (args.highlight or os.environ.get("RIOT_ID", "")).strip().strip('"')
    path = render_lobby_card(
        rows,
        output=args.output.resolve(),
        highlight_name=highlight or None,
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "output": str(path),
                "players": len(rows),
                "highlight": highlight or None,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
