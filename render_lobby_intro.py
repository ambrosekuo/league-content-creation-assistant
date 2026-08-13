#!/usr/bin/env python3
"""9:16 lobby intro variants: typography bars, vertical matchup, 3s camera story.

Works from a landscape lobby PNG (or the first frames of a weave) plus the
sidecar JSON written by generate_lobby_card.py.

Default camera story (3s): hold the full lobby, then one slow pan into
you vs the lane opponent and hold.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from generate_lobby_card import (
    ROLE_ORDER,
    loading_splash,
    lobby_content_box,
    lobby_hook,
    lobby_layout,
    player_focus_box,
    stamp_on_layout,
)

OUT_W = 1080
OUT_H = 1920
BG = (10, 12, 18)
GOLD = (255, 214, 90)
WHITE = (244, 244, 248)
MUTED = (180, 186, 198)
RED = (255, 92, 92)

FONT_BLACK = "/System/Library/Fonts/Supplemental/Arial Black.ttf"
FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FONT_REG = "/System/Library/Fonts/Supplemental/Arial.ttf"

# Camera story beats (seconds from t=0). One move: full lobby → vs crop.
STORY_FULL_END = 0.70
STORY_VS_PAN_END = 1.90
STORY_VS_HOLD = 1.00


def story_beats(duration: float) -> tuple[float, float]:
    """Return (full_end, vs_pan_end). Remaining time holds the matchup."""
    full_end = STORY_FULL_END
    vs_pan_end = min(STORY_VS_PAN_END, float(duration) - STORY_VS_HOLD)
    vs_pan_end = max(full_end + 0.50, vs_pan_end)
    return full_end, vs_pan_end


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


def draw_centered(
    draw,
    text: str,
    *,
    cy: int,
    fnt,
    fill: tuple[int, int, int],
    width: int = OUT_W,
    shadow: bool = True,
) -> None:
    tw, th = text_size(draw, text, fnt)
    x = (width - tw) // 2
    y = int(cy - th / 2)
    if shadow:
        draw.text((x + 2, y + 2), text, font=fnt, fill=(0, 0, 0))
    draw.text((x, y), text, font=fnt, fill=fill)


def clamp_box(
    x: float, y: float, w: float, h: float, src_w: int, src_h: int
) -> tuple[int, int, int, int]:
    w = max(2, min(float(src_w), w))
    h = max(2, min(float(src_h), h))
    x = max(0.0, min(x, src_w - w))
    y = max(0.0, min(y, src_h - h))
    return even(round(x)), even(round(y)), even(round(w)), even(round(h))


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def lerp_box(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    t: float,
) -> tuple[float, float, float, float]:
    """Ease the camera around frame centers with log-scale zoom.

    Lerping x/y/w/h independently expands from the top-left and rushes the
    zoom at one end — both read as jagged / nauseating.
    """
    ax, ay, aw, ah = (float(v) for v in a)
    bx, by, bw, bh = (float(v) for v in b)
    cx = lerp(ax + aw / 2.0, bx + bw / 2.0, t)
    cy = lerp(ay + ah / 2.0, by + bh / 2.0, t)
    w = math.exp(lerp(math.log(max(aw, 2.0)), math.log(max(bw, 2.0)), t))
    h = math.exp(lerp(math.log(max(ah, 2.0)), math.log(max(bh, 2.0)), t))
    return (cx - w / 2.0, cy - h / 2.0, w, h)


def ease_in_out(t: float) -> float:
    """Quintic smootherstep: zero velocity and acceleration at both ends."""
    t = max(0.0, min(1.0, t))
    return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)


def clamp_box_f(
    x: float, y: float, w: float, h: float, src_w: int, src_h: int
) -> tuple[float, float, float, float]:
    w = max(2.0, min(float(src_w), float(w)))
    h = max(2.0, min(float(src_h), float(h)))
    x = max(0.0, min(float(x), src_w - w))
    y = max(0.0, min(float(y), src_h - h))
    return x, y, w, h


def contain_crop(src, box: tuple[float, float, float, float], out_w: int = OUT_W, out_h: int = OUT_H):
    from PIL import Image

    x, y, w, h = clamp_box_f(*box, src.width, src.height)
    scale = min(out_w / max(w, 1e-6), out_h / max(h, 1e-6))
    nw = max(2, even(round(w * scale)))
    nh = max(2, even(round(h * scale)))
    region = src.transform(
        (nw, nh),
        Image.EXTENT,
        (x, y, x + w, y + h),
        Image.Resampling.BICUBIC,
    )
    canvas = Image.new("RGB", (out_w, out_h), BG)
    canvas.paste(region.convert("RGB"), ((out_w - nw) // 2, (out_h - nh) // 2))
    return canvas, (out_w - nw) // 2, (out_h - nh) // 2, nw, nh


def box_union(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x0 = min(a[0], b[0])
    y0 = min(a[1], b[1])
    x1 = max(a[0] + a[2], b[0] + b[2])
    y1 = max(a[1] + a[3], b[1] + b[3])
    return x0, y0, x1 - x0, y1 - y0


def zoom_box_for_card(
    player: dict[str, Any],
    layout: dict[str, int],
    src_w: int,
    src_h: int,
    *,
    card_frac: float = 0.42,
) -> tuple[int, int, int, int]:
    """Crop around one card so it lands at ~card_frac of portrait width.

    Height stays the card+labels, not 9:16 — a full-height 9:16 window on a
    top-row card cannot be centered without pulling in the enemy row.
    """
    focus = player_focus_box(player, layout, pad=20)
    crop_w = max(int(player["w"]) / max(card_frac, 0.15), focus[2] + 40)
    crop_h = focus[3]
    cx = focus[0] + focus[2] / 2
    cy = focus[1] + focus[3] / 2
    return clamp_box(cx - crop_w / 2, cy - crop_h / 2, crop_w, crop_h, src_w, src_h)


def content_box(meta: dict[str, Any], src_w: int, src_h: int) -> tuple[int, int, int, int]:
    layout = lobby_layout(src_w, src_h)
    box = meta.get("contentBox") or {}
    meta_layout = meta.get("layout") or {}
    same_size = int(meta_layout.get("width") or 0) == src_w and int(meta_layout.get("height") or 0) == src_h
    if box and same_size:
        return clamp_box(box["x"], box["y"], box["w"], box["h"], src_w, src_h)
    return clamp_box(*lobby_content_box(layout), src_w, src_h)


def _gold_ratio(im) -> float:
    pixels = list(im.convert("RGB").getdata())
    if not pixels:
        return 0.0
    hits = 0
    for r, g, b in pixels:
        if r >= 220 and 170 <= g <= 240 and 40 <= b <= 140 and r > g > b:
            hits += 1
    return hits / len(pixels)


def detect_highlight_slot(src) -> tuple[int, str] | None:
    """Find the gold name (you) under a lobby card. Returns (col, row)."""
    layout = lobby_layout(src.width, src.height)
    step = layout["card_w"] + layout["gap"]
    best: tuple[int, str] | None = None
    best_score = 0.015
    for row, y in (("blue", layout["blue_y"]), ("red", layout["red_y"])):
        for col in range(5):
            x = layout["x0"] + col * step
            band = src.crop(
                (
                    x,
                    y + layout["card_h"] + 4,
                    x + layout["card_w"],
                    y + layout["card_h"] + 36,
                )
            )
            score = _gold_ratio(band)
            if score > best_score:
                best_score = score
                best = (col, row)
    return best


def _slot_face(src, layout: dict[str, int], col: int, row: str):
    step = layout["card_w"] + layout["gap"]
    x = layout["x0"] + col * step
    y = layout["blue_y"] if row == "blue" else layout["red_y"]
    return src.crop(
        (
            x + 10,
            y + 20,
            x + layout["card_w"] - 10,
            y + layout["card_h"] - 70,
        )
    )


def _rgb_mse(a, b) -> float:
    pa = list(a.convert("RGB").getdata())
    pb = list(b.convert("RGB").getdata())
    n = min(len(pa), len(pb))
    if n <= 0:
        return 1e18
    acc = 0
    for i in range(n):
        r1, g1, b1 = pa[i]
        r2, g2, b2 = pb[i]
        acc += (r1 - r2) ** 2 + (g1 - g2) ** 2 + (b1 - b2) ** 2
    return acc / n


def best_champ_slot(src, champ: str) -> tuple[int, str] | None:
    """Match a DDragon loading portrait to a lobby card slot."""
    if not champ or champ in {"?", "Unknown"}:
        return None
    splash = loading_splash(champ)
    if splash is None:
        return None
    from PIL import Image

    layout = lobby_layout(src.width, src.height)
    tmpl = splash.convert("RGB").resize((48, 64), Image.Resampling.BILINEAR)
    ranked: list[tuple[float, int, str]] = []
    for row in ("blue", "red"):
        for col in range(5):
            face = _slot_face(src, layout, col, row).resize((48, 64), Image.Resampling.BILINEAR)
            ranked.append((_rgb_mse(face, tmpl), col, row))
    ranked.sort()
    if not ranked:
        return None
    best_err, col, row = ranked[0]
    second = ranked[1][0] if len(ranked) > 1 else best_err * 2
    if best_err <= second * 0.95:
        return col, row
    return None


def _real_lobby_slots(meta: dict[str, Any]) -> bool:
    players = meta.get("players") or []
    named = [
        p
        for p in players
        if str(p.get("champion") or "").strip() not in {"", "?", "Unknown"}
    ]
    me = meta.get("me") or {}
    return len(named) >= 8 and me.get("col") is not None


def align_meta_to_image(src, meta: dict[str, Any]) -> dict[str, Any]:
    """Put me/opponent boxes in this PNG's pixel space.

    Lobby cards are always 1920x1080; weaves are often 1280x720. Col/row
    plus the gold name (or splash match) on the PNG are the source of truth.
    """
    layout = lobby_layout(src.width, src.height)
    out = dict(meta)
    me = dict(out.get("me") or {})
    opp = dict(out.get("opponent") or {})
    players = [dict(p) for p in (out.get("players") or [])]
    detected = detect_highlight_slot(src)
    if detected is None and not _real_lobby_slots(out):
        me_champ = str(me.get("champion") or "")
        try:
            detected = best_champ_slot(src, me_champ)
        except Exception:
            detected = None
    if detected:
        col, row = detected
        opp_row = "red" if row == "blue" else "blue"
        role = ROLE_ORDER[col] if 0 <= col < len(ROLE_ORDER) else str(me.get("position") or "")
        slot_me = next((p for p in players if p.get("col") == col and p.get("row") == row), None)
        slot_opp = next(
            (p for p in players if p.get("col") == col and p.get("row") == opp_row), None
        )
        if slot_me and str(slot_me.get("champion") or "") not in {"", "?"}:
            me = dict(slot_me)
            me["mine"] = True
        else:
            me["col"] = col
            me["row"] = row
            me["mine"] = True
            me["position"] = role
        if slot_opp and str(slot_opp.get("champion") or "") not in {"", "?"}:
            opp = dict(slot_opp)
            opp["mine"] = False
        else:
            opp["col"] = col
            opp["row"] = opp_row
            opp["mine"] = False
            opp["position"] = role
    me = stamp_on_layout(me, layout) or me
    opp = stamp_on_layout(opp, layout) or opp
    players = [stamp_on_layout(p, layout) or p for p in players]
    cx, cy, cw, ch = lobby_content_box(layout)
    placed = {"layout": layout, "players": players, "me": me, "opponent": opp}
    out["layout"] = layout
    out["me"] = me
    out["opponent"] = opp
    out["players"] = players
    out["contentBox"] = {"x": cx, "y": cy, "w": cw, "h": ch}
    out["hook"] = lobby_hook(placed)
    return out


def crop_player(src, player: dict[str, Any], extra: int = 18):
    x = int(player["x"]) - extra
    y = int(player["y"]) - extra
    w = int(player["w"]) + extra * 2
    h = int(player["h"]) + extra * 2
    x, y, w, h = clamp_box(x, y, w, h, src.width, src.height)
    return src.crop((x, y, x + w, y + h))


def hook_from_meta(meta: dict[str, Any]) -> dict[str, Any]:
    if meta.get("hook"):
        return dict(meta["hook"])
    placed = {
        "me": meta.get("me"),
        "opponent": meta.get("opponent"),
        "players": meta.get("players") or [],
    }
    return lobby_hook(placed)


def render_bars(src, meta: dict[str, Any]):
    """Full 10-card lobby with giant hook type in the letterbox bars."""
    from PIL import Image, ImageDraw

    hook = hook_from_meta(meta)
    box = content_box(meta, src.width, src.height)
    canvas, _ox, oy, _nw, nh = contain_crop(src, box)
    draw = ImageDraw.Draw(canvas)
    top_h = oy
    bot_y = oy + nh

    title = str(hook.get("lobbyTitle") or "LOBBY")
    line_me = str(hook.get("hookMe") or "")
    line_lp = str(hook.get("hookLp") or hook.get("hookRank") or "")
    line_vs = str(hook.get("hookVs") or "")
    line_avg = str(hook.get("hookAvg") or "")

    if top_h > 80:
        f_title = fit_font(draw, title, FONT_BLACK, 72, OUT_W - 80, 28)
        draw_centered(draw, title, cy=int(top_h * 0.38), fnt=f_title, fill=GOLD)
        sub = "  •  ".join(p for p in [line_me, line_lp] if p)
        if sub:
            f_sub = fit_font(draw, sub, FONT_BOLD, 44, OUT_W - 72, 22)
            draw_centered(draw, sub, cy=int(top_h * 0.72), fnt=f_sub, fill=WHITE)

    if OUT_H - bot_y > 80:
        band_cy = bot_y + (OUT_H - bot_y) // 2
        if line_vs:
            f_vs = fit_font(draw, line_vs, FONT_BOLD, 36, OUT_W - 64, 20)
            draw_centered(draw, line_vs, cy=band_cy - (18 if line_avg else 0), fnt=f_vs, fill=WHITE)
        if line_avg:
            f_avg = fit_font(draw, line_avg, FONT_BOLD, 28, OUT_W - 80, 18)
            draw_centered(draw, line_avg, cy=band_cy + 28, fnt=f_avg, fill=MUTED)
    return canvas


def render_matchup(src, meta: dict[str, Any]):
    """Vertical you-vs-lane-opponent card; other 8 as tiny context."""
    from PIL import Image, ImageDraw

    hook = hook_from_meta(meta)
    me = meta.get("me") or {}
    opp = meta.get("opponent") or {}
    players = list(meta.get("players") or [])
    canvas = Image.new("RGB", (OUT_W, OUT_H), BG)
    draw = ImageDraw.Draw(canvas)

    title = str(hook.get("lobbyTitle") or "LOBBY")
    f_title = fit_font(draw, title, FONT_BOLD, 36, OUT_W - 80, 22)
    draw_centered(draw, title, cy=70, fnt=f_title, fill=GOLD)

    lp = str(hook.get("hookLp") or "")
    if lp:
        f_lp = fit_font(draw, lp, FONT_BLACK, 96, OUT_W - 60, 40)
        draw_centered(draw, lp, cy=150, fnt=f_lp, fill=WHITE)

    y = 220
    if me:
        card = crop_player(src, me, extra=22)
        tw = 420
        th = int(tw * card.height / max(card.width, 1))
        card = card.resize((tw, th), Image.Resampling.LANCZOS)
        canvas.paste(card.convert("RGB"), ((OUT_W - tw) // 2, y))
        y += th + 18
    me_line = str(hook.get("hookMe") or "")
    if me_line:
        f_me = fit_font(draw, me_line, FONT_BLACK, 52, OUT_W - 70, 26)
        draw_centered(draw, me_line, cy=y + 28, fnt=f_me, fill=GOLD)
        y += 64

    f_vs = font(FONT_BLACK, 42)
    draw_centered(draw, "VS", cy=y + 24, fnt=f_vs, fill=WHITE)
    y += 56

    if opp:
        card = crop_player(src, opp, extra=18)
        tw = 300
        th = int(tw * card.height / max(card.width, 1))
        card = card.resize((tw, th), Image.Resampling.LANCZOS)
        canvas.paste(card.convert("RGB"), ((OUT_W - tw) // 2, y))
        y += th + 14
        opp_bits = [
            str(opp.get("champion") or "").upper(),
            f"{int(opp['lp'])} LP" if opp.get("lp") is not None else "",
        ]
        opp_line = "  •  ".join(b for b in opp_bits if b)
        if opp_line:
            f_opp = fit_font(draw, opp_line, FONT_BOLD, 36, OUT_W - 80, 22)
            draw_centered(draw, opp_line, cy=y + 22, fnt=f_opp, fill=RED)
            y += 56

    others = [
        p
        for p in players
        if not p.get("mine")
        and not (
            opp
            and p.get("col") == opp.get("col")
            and p.get("row") == opp.get("row")
        )
    ]
    if others:
        thumb_w, thumb_h = 88, 156
        gap = 12
        n = min(8, len(others))
        row_w = n * thumb_w + (n - 1) * gap
        x0 = (OUT_W - row_w) // 2
        yy = min(OUT_H - thumb_h - 40, max(y + 20, OUT_H - 220))
        for i, p in enumerate(others[:n]):
            thumb = crop_player(src, p, extra=8).resize(
                (thumb_w, thumb_h), Image.Resampling.LANCZOS
            )
            canvas.paste(thumb.convert("RGB"), (x0 + i * (thumb_w + gap), yy))
    return canvas


def story_boxes(src, meta: dict[str, Any]) -> dict[str, tuple[int, int, int, int]]:
    layout = lobby_layout(src.width, src.height)
    full = content_box(meta, src.width, src.height)
    me = meta.get("me")
    opp = meta.get("opponent")
    me_box = zoom_box_for_card(me, layout, src.width, src.height) if me else full
    if me and opp:
        union = box_union(
            player_focus_box(me, layout, pad=12),
            player_focus_box(opp, layout, pad=12),
        )
        pad = 28
        vs_box = clamp_box(
            union[0] - pad,
            union[1] - pad,
            union[2] + 2 * pad,
            union[3] + 2 * pad,
            src.width,
            src.height,
        )
    else:
        vs_box = me_box
    return {"full": full, "me": me_box, "vs": vs_box}


def _scrim(canvas, *, top: int = 260, bot: int = 300):
    from PIL import Image, ImageDraw

    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    for i in range(max(0, top)):
        a = int(170 * (1 - i / max(top, 1)) ** 1.15)
        d.line([(0, i), (OUT_W, i)], fill=(0, 0, 0, a))
    for i in range(max(0, bot)):
        a = int(170 * (1 - i / max(bot, 1)) ** 1.15)
        y = OUT_H - 1 - i
        d.line([(0, y), (OUT_W, y)], fill=(0, 0, 0, a))
    return Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")


def story_copy(t: float, hook: dict[str, Any], *, full_end: float) -> tuple[str, str, str]:
    """(top, mid, bottom) overlay lines for the camera story."""
    if t < full_end:
        return (
            str(hook.get("lobbyTitle") or ""),
            f"{hook.get('hookMe', '')}  •  {hook.get('hookLp', '')}".strip(" •"),
            str(hook.get("hookVs") or ""),
        )
    me_lp = hook.get("hookLp") or ""
    me_champ = hook.get("meChampion") or ""
    me_role = hook.get("meRole") or ""
    opp_lp = f"{int(hook['oppLp'])} LP" if hook.get("oppLp") is not None else ""
    opp_champ = hook.get("oppChampion") or ""
    return (
        f"{me_lp} {me_champ} {me_role}".strip(),
        "VS",
        f"{opp_lp} {opp_champ}".strip(),
    )


def render_story_frame(src, meta: dict[str, Any], t: float, duration: float):
    from PIL import ImageDraw

    boxes = story_boxes(src, meta)
    hook = hook_from_meta(meta)
    full_end, vs_pan_end = story_beats(duration)
    # full lobby → one pan to you vs opponent → hold
    if t <= full_end:
        box = boxes["full"]
    elif t <= vs_pan_end:
        alpha = ease_in_out((t - full_end) / max(0.01, vs_pan_end - full_end))
        box = lerp_box(boxes["full"], boxes["vs"], alpha)
    else:
        box = boxes["vs"]

    canvas, _ox, oy, _nw, nh = contain_crop(src, box)
    top, mid, bot = story_copy(t, hook, full_end=full_end)
    top_h = oy
    bot_y = oy + nh

    if t < full_end and top_h > 80:
        draw = ImageDraw.Draw(canvas)
        if top:
            f_top = fit_font(draw, top, FONT_BLACK, 64, OUT_W - 80, 26)
            draw_centered(draw, top, cy=int(top_h * 0.38), fnt=f_top, fill=GOLD)
        if mid:
            f_mid = fit_font(draw, mid, FONT_BOLD, 40, OUT_W - 72, 22)
            draw_centered(draw, mid, cy=int(top_h * 0.72), fnt=f_mid, fill=WHITE)
        if bot:
            f_bot = fit_font(draw, bot, FONT_BOLD, 36, OUT_W - 64, 20)
            draw_centered(draw, bot, cy=bot_y + (OUT_H - bot_y) // 2, fnt=f_bot, fill=WHITE)
        return canvas

    canvas = _scrim(canvas, top=260, bot=300)
    draw = ImageDraw.Draw(canvas)
    if top:
        f_top = fit_font(draw, top, FONT_BLACK, 52, OUT_W - 70, 24)
        draw_centered(draw, top, cy=110, fnt=f_top, fill=GOLD)
    f_vs = font(FONT_BLACK, 48)
    draw_centered(draw, "VS", cy=OUT_H // 2, fnt=f_vs, fill=WHITE)
    if bot:
        f_bot = fit_font(draw, bot, FONT_BLACK, 48, OUT_W - 70, 24)
        draw_centered(draw, bot, cy=OUT_H - 130, fnt=f_bot, fill=RED)
    return canvas


def write_story_mp4(
    src,
    meta: dict[str, Any],
    output: Path,
    *,
    seconds: float = 3.0,
    fps: float = 30.0,
) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    rate = max(24, min(60, int(round(float(fps)))))
    frames = max(1, int(round(seconds * rate)))
    with tempfile.TemporaryDirectory(prefix="lobby_story_") as tmp:
        tmp_dir = Path(tmp)
        for i in range(frames):
            t = i / rate
            frame = render_story_frame(src, meta, t, seconds)
            frame.save(tmp_dir / f"f{i:04d}.jpg", quality=92)
        cmd = [
            "ffmpeg",
            "-y",
            "-framerate",
            str(rate),
            "-i",
            str(tmp_dir / "f%04d.jpg"),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "18",
            "-preset",
            "veryfast",
            "-movflags",
            "+faststart",
            str(output),
        ]
        proc = subprocess.run(cmd, text=True, capture_output=True)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"ffmpeg story failed\n{detail}")
    return output


def extract_lobby_png(video: Path, output: Path, *, at: float = 0.8) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{at:.3f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-update",
        "1",
        str(output),
    ]
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or "ffmpeg extract failed")
    return output


def resolve_lobby_assets(
    source: Path,
    lobby_png: Path | None = None,
    lobby_meta: Path | None = None,
) -> tuple[Path | None, Path | None]:
    png = lobby_png if lobby_png and lobby_png.is_file() else None
    meta = lobby_meta if lobby_meta and lobby_meta.is_file() else None
    if png is None:
        for cand in (
            source.with_name(f"{source.stem}_lobby.png"),
            source.parent / f"{source.stem}_lobby.png",
        ):
            if cand.is_file():
                png = cand
                break
    if meta is None and png is not None:
        cand = png.with_name(f"{png.stem}_meta.json")
        if cand.is_file():
            meta = cand
    if meta is None:
        cand = source.with_name(f"{source.stem}_lobby_meta.json")
        if cand.is_file():
            meta = cand
    return png, meta


def synthetic_meta(
    src_w: int,
    src_h: int,
    *,
    me_champion: str = "Unknown",
    me_role: str = "JUNGLE",
    me_tier: str = "GRANDMASTER",
    me_lp: int | None = None,
    me_col: int = 1,
    me_row: str = "blue",
    opp_champion: str = "",
    opp_tier: str = "GRANDMASTER",
    opp_lp: int | None = None,
) -> dict[str, Any]:
    layout = lobby_layout(src_w, src_h)
    step = layout["card_w"] + layout["gap"]
    me_y = layout["blue_y"] if me_row == "blue" else layout["red_y"]
    opp_row = "red" if me_row == "blue" else "blue"
    opp_y = layout["red_y"] if me_row == "blue" else layout["blue_y"]
    me = {
        "champion": me_champion,
        "position": me_role,
        "tier": me_tier,
        "lp": me_lp,
        "col": int(me_col),
        "row": me_row,
        "x": layout["x0"] + int(me_col) * step,
        "y": me_y,
        "w": layout["card_w"],
        "h": layout["card_h"],
        "mine": True,
        "teamId": 100 if me_row == "blue" else 200,
    }
    opp = {
        "champion": opp_champion,
        "position": me_role,
        "tier": opp_tier,
        "lp": opp_lp,
        "col": int(me_col),
        "row": opp_row,
        "x": layout["x0"] + int(me_col) * step,
        "y": opp_y,
        "w": layout["card_w"],
        "h": layout["card_h"],
        "mine": False,
        "teamId": 200 if me_row == "blue" else 100,
    }
    players = []
    for row, y, team in (("blue", layout["blue_y"], 100), ("red", layout["red_y"], 200)):
        for col in range(5):
            if row == me_row and col == int(me_col):
                players.append(me)
            elif row == opp_row and col == int(me_col):
                players.append(opp)
            else:
                players.append(
                    {
                        "champion": "?",
                        "position": "",
                        "col": col,
                        "row": row,
                        "x": layout["x0"] + col * step,
                        "y": y,
                        "w": layout["card_w"],
                        "h": layout["card_h"],
                        "mine": False,
                        "teamId": team,
                    }
                )
    placed = {"layout": layout, "players": players, "me": me, "opponent": opp}
    cx, cy, cw, ch = lobby_content_box(layout)
    return {
        "layout": layout,
        "contentBox": {"x": cx, "y": cy, "w": cw, "h": ch},
        "me": me,
        "opponent": opp,
        "players": players,
        "hook": lobby_hook(placed),
    }


def meta_from_source_name(name: str, src_w: int, src_h: int) -> dict[str, Any]:
    """Best-effort me/opp champs from gam01_leblanc_vs_nocturne_loss-style names."""
    stem = Path(name).stem.lower()
    me_champ = "Unknown"
    opp_champ = ""
    body = stem
    if "_" in body:
        prefix, rest = body.split("_", 1)
        if prefix.startswith("gam") or (prefix.startswith("g") and prefix[1:].isdigit()):
            body = rest
    if "_vs_" in body:
        left, right = body.split("_vs_", 1)
        me_champ = left.replace("_", " ").strip() or "Unknown"
        opp_champ = right.split("_")[0].replace("_", " ").strip()
    return synthetic_meta(
        src_w,
        src_h,
        me_champion=me_champ.title() if me_champ else "Unknown",
        opp_champion=opp_champ.title() if opp_champ else "",
    )


def build_story_video(
    *,
    lobby_image: Path,
    meta: dict[str, Any],
    output: Path,
    seconds: float = 3.0,
    fps: float = 30.0,
) -> Path:
    from PIL import Image

    src = Image.open(lobby_image).convert("RGB")
    meta = align_meta_to_image(src, meta)
    return write_story_mp4(src, meta, output, seconds=seconds, fps=fps)


def meta_from_args(args: argparse.Namespace, src_w: int, src_h: int) -> dict[str, Any]:
    if args.meta and args.meta.is_file():
        return load_json(args.meta)
    return synthetic_meta(
        src_w,
        src_h,
        me_champion=str(args.me_champion),
        me_role=str(args.me_role),
        me_tier=str(args.me_tier),
        me_lp=args.me_lp,
        me_col=int(args.me_col),
        me_row=str(args.me_row),
        opp_champion=str(args.opp_champion),
        opp_tier=str(args.opp_tier),
        opp_lp=args.opp_lp,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Render 9:16 lobby intro iterations.")
    p.add_argument("--lobby", type=Path, default=None, help="Landscape lobby PNG")
    p.add_argument("--video", type=Path, default=None, help="Weave mp4; extract lobby frame")
    p.add_argument("--meta", type=Path, default=None, help="Sidecar JSON from generate_lobby_card")
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument(
        "--variants",
        default="bars,matchup,story",
        help="Comma list: bars,matchup,story",
    )
    p.add_argument("--seconds", type=float, default=3.0)
    p.add_argument("--me-champion", default="Leblanc")
    p.add_argument("--me-role", default="JUNGLE")
    p.add_argument("--me-tier", default="GRANDMASTER")
    p.add_argument("--me-lp", type=int, default=1050)
    p.add_argument("--me-col", type=int, default=1, help="0=top … 4=support")
    p.add_argument("--me-row", choices=("blue", "red"), default="blue")
    p.add_argument("--opp-champion", default="Nocturne")
    p.add_argument("--opp-tier", default="GRANDMASTER")
    p.add_argument("--opp-lp", type=int, default=1012)
    return p


def main(argv: list[str] | None = None) -> int:
    from PIL import Image

    args = build_parser().parse_args(argv)
    out_dir: Path = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    lobby_png = args.lobby
    if lobby_png is None:
        if args.video is None or not args.video.is_file():
            print("error: pass --lobby PNG or --video weave", file=sys.stderr)
            return 1
        lobby_png = out_dir / "lobby_source.png"
        extract_lobby_png(args.video.resolve(), lobby_png)

    src = Image.open(lobby_png).convert("RGB")
    meta = align_meta_to_image(src, meta_from_args(args, src.width, src.height))
    (out_dir / "intro_meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    variants = {v.strip().lower() for v in str(args.variants).split(",") if v.strip()}
    written: dict[str, str] = {}
    if "bars" in variants:
        path = out_dir / "01_bars.jpg"
        render_bars(src, meta).save(path, quality=92)
        written["bars"] = str(path)
    if "matchup" in variants:
        path = out_dir / "02_matchup.jpg"
        render_matchup(src, meta).save(path, quality=92)
        written["matchup"] = str(path)
    if "story" in variants:
        mp4 = out_dir / "03_story.mp4"
        write_story_mp4(src, meta, mp4, seconds=float(args.seconds))
        written["story"] = str(mp4)
        for label, t in (("full", 0.35), ("vs", 2.50)):
            frame = render_story_frame(src, meta, t, float(args.seconds))
            path = out_dir / f"03_story_{label}.jpg"
            frame.save(path, quality=92)
            written[f"story_{label}"] = str(path)

    print(json.dumps({"status": "ok", "out_dir": str(out_dir), "outputs": written}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
