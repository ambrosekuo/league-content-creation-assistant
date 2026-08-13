#!/usr/bin/env python3
"""Render TikTok/Twitch-style 9:16 portrait clips from landscape stream footage.

Layout (default):
  ┌─────────────────┐
  │     facecam     │  ← crop from bottom-right of source
  ├─────────────────┤
  │            [KDA]│  ← optional PIP from source top-right
  │    gameplay     │  ← zoomed center crop
  │                 │
  └─────────────────┘

Calibrate --cam-* / --kda-* once from a mid-frame; both are assumed static.

For weaves with a lobby-card intro, pass --still-seconds 3 so the opening
is shown without the facecam split. Default --still-mode story plays the
animated lobby intro (full lobby → you → matchup) then smash-cuts to gameplay.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{detail}")


def probe(path: Path) -> dict[str, Any]:
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
    duration = float((data.get("format") or {}).get("duration") or 0.0)
    rate = str(stream.get("r_frame_rate") or "30/1")
    fps = 30.0
    if "/" in rate:
        num, den = rate.split("/", 1)
        try:
            fps = float(num) / max(float(den), 1.0)
        except ValueError:
            fps = 30.0
    return {
        "width": int(stream.get("width") or 1920),
        "height": int(stream.get("height") or 1080),
        "duration": duration,
        "fps": max(1.0, min(fps, 60.0)),
    }


def even(n: int) -> int:
    return n - (n % 2)


# Crop boxes were calibrated on 1920x1080 Twitch VODs. Scale them when the
# weave is a different size (e.g. 1280x720) so clamps don't trash the layout.
REF_W = 1920
REF_H = 1080
# Lobby card row: 5*168 + 4*28 = 952px, plus rank-wing overhang (~29px/side)
# and a little frame pad. Centered in the 1920-wide still.
STILL_CHAMPS_REF_W = 1040
STILL_PAD_COLOR = "0x0A0C12"


def scale_box(
    x: int,
    y: int,
    w: int,
    h: int,
    *,
    src_w: int,
    src_h: int,
    ref_w: int = REF_W,
    ref_h: int = REF_H,
) -> tuple[int, int, int, int]:
    """Scale a crop box from ref resolution into src resolution."""
    if src_w <= 0 or src_h <= 0:
        return x, y, w, h
    if src_w == ref_w and src_h == ref_h:
        return x, y, w, h
    sx = src_w / float(ref_w)
    sy = src_h / float(ref_h)
    nx = int(round(x * sx))
    ny = int(round(y * sy))
    nw = even(max(2, int(round(w * sx))))
    nh = even(max(2, int(round(h * sy))))
    nx = max(0, min(nx, max(0, src_w - nw)))
    ny = max(0, min(ny, max(0, src_h - nh)))
    nw = even(max(2, min(nw, src_w - nx)))
    nh = even(max(2, min(nh, src_h - ny)))
    return nx, ny, nw, nh


def _prepare_story_intro(
    source: Path,
    output: Path,
    *,
    src_w: int,
    src_h: int,
    still_seconds: float,
    fps: float,
    lobby_png: Path | None,
    lobby_meta: Path | None,
) -> Path:
    from render_lobby_intro import (
        build_story_video,
        extract_lobby_png,
        load_json,
        meta_from_source_name,
        resolve_lobby_assets,
    )

    png, meta_path = resolve_lobby_assets(source, lobby_png, lobby_meta)
    if png is None:
        png = output.with_name(f"{source.stem}_lobby_frame.png")
        extract_lobby_png(source, png, at=min(0.8, max(0.2, still_seconds * 0.35)))
        print(f"[portrait] story: extracted lobby frame {png.name}", flush=True)
    if meta_path is not None:
        meta = load_json(meta_path)
        print(f"[portrait] story: using meta {meta_path.name}", flush=True)
    else:
        from PIL import Image as _Image

        with _Image.open(png) as im:
            meta = meta_from_source_name(source.name, im.width, im.height)
        print("[portrait] story: no lobby meta sidecar; inferred from filename + lobby PNG", flush=True)
    story_path = output.with_name(f"{output.stem}_lobby_story.mp4")
    build_story_video(
        lobby_image=png,
        meta=meta,
        output=story_path,
        seconds=still_seconds,
        fps=fps,
    )
    print(f"[portrait] story intro {story_path.name} ({still_seconds:.1f}s)", flush=True)
    return story_path


def still_champs_crop(src_w: int, src_h: int) -> tuple[int, int, int, int]:
    """Centered crop of the 10-champion lobby block; keep full source height."""
    frac = STILL_CHAMPS_REF_W / float(REF_W)
    crop_w = even(max(2, int(round(src_w * frac))))
    crop_w = min(crop_w, even(max(2, src_w)))
    crop_h = even(max(2, src_h))
    crop_x = max(0, (src_w - crop_w) // 2)
    crop_y = 0
    crop_x = min(crop_x, max(0, src_w - crop_w))
    crop_h = min(crop_h, src_h - crop_y)
    crop_w = even(max(2, min(crop_w, src_w - crop_x)))
    crop_h = even(max(2, min(crop_h, src_h - crop_y)))
    return crop_x, crop_y, crop_w, crop_h


def still_fit_vf(
    out_w: int,
    out_h: int,
    *,
    mode: str = "champs",
    src_w: int = REF_W,
    src_h: int = REF_H,
) -> str:
    """Place landscape lobby still into portrait.

    champs  = center-crop to the 10-champion block, then fit (all champs, max size)
    contain = letterbox (see whole lobby, bars on sides/top)
    cover   = crop to fill 9:16 (no bars; sides of lobby get cut)
    """
    mode = (mode or "champs").strip().lower()
    if mode == "cover":
        return (
            f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h},"
            f"setsar=1,format=yuv420p"
        )
    if mode in {"champs", "cards", "content"}:
        crop_x, crop_y, crop_w, crop_h = still_champs_crop(src_w, src_h)
        return (
            f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},"
            f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
            f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color={STILL_PAD_COLOR},"
            f"setsar=1,format=yuv420p"
        )
    return (
        f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
        f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color={STILL_PAD_COLOR},"
        f"setsar=1,format=yuv420p"
    )


def facecam_band(
    *,
    src_w: int,
    src_h: int,
    cam_w: int,
    cam_h: int,
    cam_x: int,
    cam_y: int,
    out_w: int,
    out_h: int,
    cam_fraction: float,
) -> tuple[str, int, int, int, int, int, int]:
    """Return (cam_vf, top_h, bot_h, cam_w, cam_h, cam_x, cam_y)."""
    out_w = even(out_w)
    out_h = even(out_h)
    cam_w = even(max(2, cam_w))
    cam_h = even(max(2, cam_h))
    cam_x = max(0, min(cam_x, src_w - cam_w))
    cam_y = max(0, min(cam_y, src_h - cam_h))

    top_h = even(max(2, int(round(out_h * cam_fraction))))
    cam_scaled_h = even(max(2, int(round(out_w * cam_h / cam_w))))
    # Prefer filling width with no letterbox under the cam (no black "border" band).
    if cam_scaled_h <= top_h:
        top_h = cam_scaled_h
        cam_vf = (
            f"crop={cam_w}:{cam_h}:{cam_x}:{cam_y},"
            f"scale={out_w}:{cam_scaled_h},"
            f"setsar=1"
        )
    else:
        cam_vf = (
            f"crop={cam_w}:{cam_h}:{cam_x}:{cam_y},"
            f"scale=-2:{top_h},"
            f"pad={out_w}:{top_h}:(ow-iw)/2:0:color=black,"
            f"setsar=1"
        )
    bot_h = even(out_h - top_h)
    if top_h + bot_h != out_h:
        bot_h = out_h - top_h
    return cam_vf, top_h, bot_h, cam_w, cam_h, cam_x, cam_y


def gameplay_crop_box(
    *,
    src_w: int,
    src_h: int,
    cam_w: int,
    cam_h: int,
    cam_x: int,
    cam_y: int,
    out_w: int,
    out_h: int,
    cam_fraction: float,
    game_mode: str = "crop",
    game_zoom: float = 1.0,
) -> dict[str, int] | None:
    """Horizontal pan box for the gameplay pane, or None if the layout isn't a simple crop."""
    _cam_vf, _top_h, bot_h, cam_w, cam_h, cam_x, cam_y = facecam_band(
        src_w=src_w,
        src_h=src_h,
        cam_w=cam_w,
        cam_h=cam_h,
        cam_x=cam_x,
        cam_y=cam_y,
        out_w=out_w,
        out_h=out_h,
        cam_fraction=cam_fraction,
    )
    mode = (game_mode or "crop").strip().lower()
    zoom = max(0.0, min(1.0, float(game_zoom)))
    if mode == "fit" or zoom <= 0.0 or bot_h <= 0:
        return None
    game_aspect = out_w / bot_h
    game_crop_h = src_h
    tight_w = even(max(2, int(round(game_crop_h * game_aspect))))
    if tight_w > src_w:
        tight_w = even(src_w)
        game_crop_h = even(max(2, int(round(tight_w / game_aspect))))
    game_crop_w = even(
        max(tight_w, int(round(tight_w + (src_w - tight_w) * (1.0 - zoom))))
    )
    game_crop_w = even(min(src_w, max(2, game_crop_w)))
    # Keep the 9:16 window inside the map art. Facecam sits in the
    # bottom-right, but fights often happen above it — allow the crop
    # to slide right far enough to keep a nearby enemy on screen.
    min_x = even(max(0, int(round(src_w * 0.08))))
    right_pad = even(max(16, int(round(src_w * 0.055))))
    max_x = even(max(0, src_w - game_crop_w - right_pad))
    if max_x < min_x:
        min_x = max_x
    center_x = max(0, (src_w - game_crop_w) // 2)
    game_x = min(center_x, max_x)
    game_x = max(min_x, min(game_x, max_x))
    game_y = max(0, (src_h - game_crop_h) // 2)
    return {
        "crop_w": game_crop_w,
        "crop_h": game_crop_h,
        "crop_x": game_x,
        "crop_y": game_y,
        "min_x": min_x,
        "max_x": max_x,
        "scale_fill": 1 if (game_crop_w / max(game_crop_h, 1) > game_aspect + 0.01) else 0,
        "cam_x": cam_x,
        "cam_y": cam_y,
        "cam_w": cam_w,
        "cam_h": cam_h,
    }


def cam_hole_prefix(
    mode: str,
    *,
    cam_x: int,
    cam_y: int,
    cam_w: int,
    cam_h: int,
) -> str:
    """Prefix on the gameplay stream for the source facecam rectangle.

    black — paint it out (legacy). keep — leave the webcam. fill — cover
    with a blurred copy of the map immediately above the webcam.
    """
    kind = (mode or "black").strip().lower()
    if kind in {"keep", "facecam", "none"}:
        return ""
    if kind in {"fill", "cover"}:
        fy = even(max(0, cam_y - cam_h))
        return (
            f"split=2[gh][gf];"
            f"[gf]crop={cam_w}:{cam_h}:{cam_x}:{fy},"
            f"scale={cam_w}:{cam_h},boxblur=10:2[hole];"
            f"[gh][hole]overlay=x={cam_x}:y={cam_y}[gfilled];"
            f"[gfilled]"
        )
    return f"drawbox=x={cam_x}:y={cam_y}:w={cam_w}:h={cam_h}:color=black:t=fill,"


def cam_stack_parts(
    *,
    src_w: int,
    src_h: int,
    cam_w: int,
    cam_h: int,
    cam_x: int,
    cam_y: int,
    out_w: int,
    out_h: int,
    cam_fraction: float,
    game_mode: str = "crop",
    game_zoom: float = 1.0,
    crop_x: int | None = None,
    sendcmd_path: Path | None = None,
    cam_hole: str = "fill",
) -> tuple[str, str, int, int]:
    """Return (cam_vf, game_vf, top_h, bot_h) for the facecam stack.

    game_mode:
      crop — vertical center crop (zoomed action)
      fit  — scale full 16:9 HUD into the game pane

    game_zoom (crop mode only):
      1.0 = tight fill (current). Lower = zoom out toward full frame width
      (0.0 ≈ use full source width, letterbox/pillar as needed via scale).
    """
    out_w = even(out_w)
    out_h = even(out_h)
    cam_vf, top_h, bot_h, cam_w, cam_h, cam_x, cam_y = facecam_band(
        src_w=src_w,
        src_h=src_h,
        cam_w=cam_w,
        cam_h=cam_h,
        cam_x=cam_x,
        cam_y=cam_y,
        out_w=out_w,
        out_h=out_h,
        cam_fraction=cam_fraction,
    )

    mode = (game_mode or "crop").strip().lower()
    hole = cam_hole_prefix(
        cam_hole, cam_x=cam_x, cam_y=cam_y, cam_w=cam_w, cam_h=cam_h
    )
    zoom = max(0.0, min(1.0, float(game_zoom)))

    if mode == "fit" or zoom <= 0.0:
        game_vf = (
            f"{hole}"
            f"scale={out_w}:{bot_h}:force_original_aspect_ratio=decrease,"
            f"pad={out_w}:{bot_h}:(ow-iw)/2:(oh-ih)/2:color=0x0A0C12,"
            f"setsar=1"
        )
    else:
        box = gameplay_crop_box(
            src_w=src_w,
            src_h=src_h,
            cam_w=cam_w,
            cam_h=cam_h,
            cam_x=cam_x,
            cam_y=cam_y,
            out_w=out_w,
            out_h=out_h,
            cam_fraction=cam_fraction,
            game_mode=mode,
            game_zoom=zoom,
        )
        if box is None:
            game_vf = (
                f"{hole}"
                f"scale={out_w}:{bot_h}:force_original_aspect_ratio=decrease,"
                f"pad={out_w}:{bot_h}:(ow-iw)/2:(oh-ih)/2:color=0x0A0C12,"
                f"setsar=1"
            )
        else:
            game_x = box["crop_x"] if crop_x is None else int(crop_x)
            game_x = max(0, min(even(game_x), src_w - box["crop_w"]))
            crop = f"crop={box['crop_w']}:{box['crop_h']}:{game_x}:{box['crop_y']}"
            if sendcmd_path is not None:
                from track_champion import ffmpeg_escape_path

                crop = (
                    f"sendcmd=f='{ffmpeg_escape_path(sendcmd_path)}',"
                    f"crop@game={box['crop_w']}:{box['crop_h']}:{game_x}:{box['crop_y']}"
                )
            if box.get("scale_fill"):
                game_vf = (
                    f"{hole}"
                    f"{crop},"
                    f"scale={out_w}:{bot_h}:force_original_aspect_ratio=increase,"
                    f"crop={out_w}:{bot_h},"
                    f"setsar=1"
                )
            else:
                game_vf = (
                    f"{hole}"
                    f"{crop},"
                    f"scale={out_w}:{bot_h},"
                    f"setsar=1"
                )
    return cam_vf, game_vf, top_h, bot_h


def build_filter(
    *,
    src_w: int,
    src_h: int,
    cam_w: int,
    cam_h: int,
    cam_x: int,
    cam_y: int,
    out_w: int,
    out_h: int,
    cam_fraction: float,
    still_seconds: float = 0.0,
    still_mode: str = "story",
    still_from_input: int | None = None,
    fps: float = 30.0,
    game_mode: str = "crop",
    game_zoom: float = 1.0,
    crop_x: int | None = None,
    sendcmd_path: Path | None = None,
    cam_hole: str = "fill",
    kda_overlay: bool = True,
    kda_x: int = 1316,
    kda_y: int = 4,
    kda_w: int = 520,
    kda_h: int = 32,
    kda_out_w: int = 560,
    kda_margin: int = 0,
) -> str:
    """Build filter graph with optional lobby still + KDA PIP overlay."""
    out_w = even(out_w)
    out_h = even(out_h)
    cam_x, cam_y, cam_w, cam_h = scale_box(
        cam_x, cam_y, cam_w, cam_h, src_w=src_w, src_h=src_h
    )
    kda_x, kda_y, kda_w, kda_h = scale_box(
        kda_x, kda_y, kda_w, kda_h, src_w=src_w, src_h=src_h
    )
    cam_vf, game_vf, top_h, _bot_h = cam_stack_parts(
        src_w=src_w,
        src_h=src_h,
        cam_w=cam_w,
        cam_h=cam_h,
        cam_x=cam_x,
        cam_y=cam_y,
        out_w=out_w,
        out_h=out_h,
        cam_fraction=cam_fraction,
        game_mode=game_mode,
        game_zoom=game_zoom,
        crop_x=crop_x,
        sendcmd_path=sendcmd_path,
        cam_hole=cam_hole,
    )
    still_s = max(0.0, float(still_seconds))

    kda_w = even(max(2, min(kda_w, src_w - max(0, kda_x))))
    kda_h = even(max(2, min(kda_h, src_h - max(0, kda_y))))
    kda_x = max(0, min(kda_x, src_w - kda_w))
    kda_y = max(0, min(kda_y, src_h - kda_h))
    kda_out_w = even(max(2, min(kda_out_w, out_w - 2 * max(kda_margin, 0))))
    kda_out_h = even(max(2, int(round(kda_out_w * kda_h / max(kda_w, 1)))))
    # Flush to top-right of the full portrait frame (right edge of HUD = right edge of video)
    ov_x = out_w - kda_out_w - max(0, kda_margin)
    ov_y = top_h + max(0, kda_margin)

    # Tight crop only — no plate/border
    kda_vf = (
        f"crop={kda_w}:{kda_h}:{kda_x}:{kda_y},"
        f"scale={kda_out_w}:{kda_out_h},"
        f"setsar=1"
    )

    def stack_with_optional_kda(split_n: int, cam_label: str, game_label: str, kda_label: str | None) -> str:
        """cam+game vstack, optionally overlay KDA PIP → [v] or intermediate."""
        body = (
            f"[{cam_label}]{cam_vf}[cam];"
            f"[{game_label}]{game_vf}[game];"
            f"[cam][game]vstack=inputs=2[base]"
        )
        if kda_overlay and kda_label:
            body += (
                f";[{kda_label}]{kda_vf}[kda];"
                f"[base][kda]overlay=x=main_w-overlay_w-{max(0, kda_margin)}:y={ov_y}:format=auto,format=yuv420p[v]"
            )
        else:
            body += ",format=yuv420p[v]"
        return body

    if still_s <= 0:
        if kda_overlay:
            return (
                f"[0:v]split=3[cam_src][game_src][kda_src];"
                + stack_with_optional_kda(3, "cam_src", "game_src", "kda_src")
            )
        return (
            f"[0:v]split=2[cam_src][game_src];"
            + stack_with_optional_kda(2, "cam_src", "game_src", None)
        )

    play_head = "[0:v]" if still_from_input is not None else "[v_play]"
    if kda_overlay:
        play = (
            f"{play_head}trim=start={still_s:.3f},setpts=PTS-STARTPTS,split=3[cam_src][game_src][kda_src];"
            f"[cam_src]{cam_vf}[cam];"
            f"[game_src]{game_vf}[game];"
            f"[cam][game]vstack=inputs=2[base];"
            f"[kda_src]{kda_vf}[kda];"
            f"[base][kda]overlay=x=main_w-overlay_w-{max(0, kda_margin)}:y={ov_y}:format=auto,format=yuv420p[play]"
        )
    else:
        play = (
            f"{play_head}trim=start={still_s:.3f},setpts=PTS-STARTPTS,split=2[cam_src][game_src];"
            f"[cam_src]{cam_vf}[cam];"
            f"[game_src]{game_vf}[game];"
            f"[cam][game]vstack=inputs=2,format=yuv420p[play]"
        )

    if still_from_input is not None:
        idx = int(still_from_input)
        fps_s = f"{max(1.0, float(fps)):.3f}"
        still = (
            f"[{idx}:v]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
            f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color={STILL_PAD_COLOR},"
            f"setsar=1,fps={fps_s},format=yuv420p,"
            f"trim=duration={still_s:.3f},setpts=PTS-STARTPTS[still];"
        )
        return still + play + ";[still][play]concat=n=2:v=1:a=0[v]"

    fit = still_fit_vf(out_w, out_h, mode=still_mode, src_w=src_w, src_h=src_h)
    return (
        f"[0:v]split=2[v_still][v_play];"
        f"[v_still]trim=duration={still_s:.3f},setpts=PTS-STARTPTS,{fit}[still];"
        f"{play};"
        f"[still][play]concat=n=2:v=1:a=0[v]"
    )


def render_portrait(
    source: Path,
    output: Path,
    *,
    cam_w: int,
    cam_h: int,
    cam_x: int,
    cam_y: int,
    out_w: int = 1080,
    out_h: int = 1920,
    cam_fraction: float = 0.34,
    still_seconds: float = 0.0,
    still_mode: str = "story",
    lobby_png: Path | None = None,
    lobby_meta: Path | None = None,
    game_mode: str = "crop",
    game_zoom: float = 1.0,
    cam_hole: str = "fill",
    kda_overlay: bool = True,
    kda_x: int = 1316,
    kda_y: int = 4,
    kda_w: int = 520,
    kda_h: int = 32,
    kda_out_w: int = 560,
    kda_margin: int = 0,
    start: float | None = None,
    duration: float | None = None,
    crf: int = 20,
    preset: str = "veryfast",
    preview_frame: Path | None = None,
    track_champion: bool = False,
    track_fps: float = 4.0,
    track_dead_zone: float = 0.10,
    track_ease_ms: float = 280.0,
    track_max_speed: float = 860.0,
    track_self_bias: float = 0.50,
    track_enemy_pull: float = 0.45,
    track_pan_cooldown: float = 0.7,
    track_outside_hold: float = 0.12,
    track_debug_dir: Path | None = None,
) -> dict[str, Any]:
    info = probe(source)
    src_w = int(info["width"])
    src_h = int(info["height"])
    cam_sx, cam_sy, cam_sw, cam_sh = scale_box(
        cam_x, cam_y, cam_w, cam_h, src_w=src_w, src_h=src_h
    )
    filt_kwargs: dict[str, Any] = dict(
        src_w=src_w,
        src_h=src_h,
        cam_w=cam_w,
        cam_h=cam_h,
        cam_x=cam_x,
        cam_y=cam_y,
        out_w=out_w,
        out_h=out_h,
        cam_fraction=cam_fraction,
        still_mode=still_mode,
        game_mode=game_mode,
        game_zoom=game_zoom,
        cam_hole=cam_hole,
        kda_overlay=kda_overlay,
        kda_x=kda_x,
        kda_y=kda_y,
        kda_w=kda_w,
        kda_h=kda_h,
        kda_out_w=kda_out_w,
        kda_margin=kda_margin,
    )
    track_report: dict[str, Any] | None = None
    if track_champion:
        box = gameplay_crop_box(
            src_w=src_w,
            src_h=src_h,
            cam_w=cam_sw,
            cam_h=cam_sh,
            cam_x=cam_sx,
            cam_y=cam_sy,
            out_w=out_w,
            out_h=out_h,
            cam_fraction=cam_fraction,
            game_mode=game_mode,
            game_zoom=game_zoom,
        )
        if box is None:
            print(
                "[track] skip: this game-mode/zoom is not a simple horizontal crop",
                flush=True,
            )
        else:
            from track_champion import track_for_portrait

            sendcmd_path = output.with_name(output.stem + "_track.cmd")
            dump_path = output.with_name(output.stem + "_track.json")
            track_report = track_for_portrait(
                source,
                src_w=src_w,
                src_h=src_h,
                crop_w=box["crop_w"],
                min_x=box["min_x"],
                max_x=box["max_x"],
                init_x=box["crop_x"],
                start=start,
                duration=duration,
                still_seconds=still_seconds,
                fps=track_fps,
                dead_zone=track_dead_zone,
                ease_s=max(0.05, float(track_ease_ms) / 1000.0),
                max_speed_px_s=float(track_max_speed),
                self_bias=float(track_self_bias),
                enemy_pull=float(track_enemy_pull),
                pan_cooldown_s=float(track_pan_cooldown),
                outside_hold_s=float(track_outside_hold),
                sendcmd_path=sendcmd_path,
                dump_path=dump_path,
                debug_dir=track_debug_dir,
            )
            filt_kwargs["sendcmd_path"] = sendcmd_path
            filt_kwargs["crop_x"] = int(track_report["first_crop_x"])

    story_mp4: Path | None = None
    if str(still_mode).strip().lower() == "story" and still_seconds > 0:
        try:
            story_mp4 = _prepare_story_intro(
                source,
                output,
                src_w=src_w,
                src_h=src_h,
                still_seconds=still_seconds,
                fps=float(info["fps"]),
                lobby_png=lobby_png,
                lobby_meta=lobby_meta,
            )
            filt_kwargs["still_from_input"] = 1
            filt_kwargs["fps"] = float(info["fps"])
        except Exception as exc:
            print(f"[portrait] story intro failed ({exc}); falling back to champs", flush=True)
            filt_kwargs["still_mode"] = "champs"
            story_mp4 = None

    filt = build_filter(still_seconds=still_seconds, **filt_kwargs)

    output.parent.mkdir(parents=True, exist_ok=True)

    if preview_frame is not None:
        preview_frame.parent.mkdir(parents=True, exist_ok=True)
        base = start if start is not None else 0.0
        ss = base + max(still_seconds + 1.0, (info["duration"] - base) * 0.5)
        preview_kwargs = dict(filt_kwargs)
        preview_kwargs.pop("sendcmd_path", None)
        preview_kwargs.pop("still_from_input", None)
        if track_report and track_report.get("path"):
            play_t = max(0.0, ss - base - still_seconds)
            crop_at = int(track_report["path"][0]["x"])
            for pt in track_report["path"]:
                if float(pt["t"]) <= play_t:
                    crop_at = int(pt["x"])
                else:
                    break
            preview_kwargs["crop_x"] = crop_at
        preview_filt = build_filter(still_seconds=0.0, **preview_kwargs)
        run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{ss:.3f}",
                "-i",
                str(source),
                "-filter_complex",
                preview_filt,
                "-map",
                "[v]",
                "-frames:v",
                "1",
                str(preview_frame),
            ]
        )

    cmd = ["ffmpeg", "-y"]
    if start is not None and start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(source)]
    if story_mp4 is not None:
        cmd += ["-i", str(story_mp4)]
    if duration is not None and duration > 0:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += [
        "-filter_complex",
        filt,
        "-map",
        "[v]",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-threads",
        "0",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-shortest",
        "-movflags",
        "+faststart",
        str(output),
    ]
    run(cmd)
    out_info = probe(output)
    return {
        "source": str(source),
        "output": str(output),
        "duration": out_info["duration"],
        "width": out_info["width"],
        "height": out_info["height"],
        "cam": {"w": cam_w, "h": cam_h, "x": cam_x, "y": cam_y},
        "cam_fraction": cam_fraction,
        "still_seconds": still_seconds,
        "still_mode": filt_kwargs.get("still_mode", still_mode),
        "story_intro": str(story_mp4) if story_mp4 else None,
        "game_mode": game_mode,
        "kda_overlay": kda_overlay,
        "kda": {"x": kda_x, "y": kda_y, "w": kda_w, "h": kda_h, "out_w": kda_out_w},
        "preview_frame": str(preview_frame) if preview_frame else None,
        "track": (
            {
                "lock_ratio": track_report.get("lock_ratio"),
                "locked": track_report.get("locked"),
                "samples": track_report.get("samples"),
                "sendcmd": track_report.get("sendcmd"),
            }
            if track_report
            else None
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Render 9:16 portrait (facecam top + gameplay + KDA PIP).")
    p.add_argument("--input", type=Path, required=True, help="Landscape source mp4")
    p.add_argument("--output", type=Path, required=True, help="Portrait output mp4")
    p.add_argument("--cam-w", type=int, default=584, help="Facecam crop width")
    p.add_argument("--cam-h", type=int, default=328, help="Facecam crop height")
    p.add_argument("--cam-x", type=int, default=1336, help="Facecam crop left")
    p.add_argument("--cam-y", type=int, default=752, help="Facecam crop top")
    p.add_argument("--width", type=int, default=1080, help="Output width")
    p.add_argument("--height", type=int, default=1920, help="Output height")
    p.add_argument(
        "--cam-fraction",
        type=float,
        default=0.34,
        help="Fraction of portrait height for facecam band (default 0.34)",
    )
    p.add_argument(
        "--still-seconds",
        type=float,
        default=0.0,
        help="Opening seconds treated as lobby still; no facecam split",
    )
    p.add_argument(
        "--still-mode",
        choices=("story", "champs", "contain", "cover"),
        default="story",
        help="Lobby intro: story=animated camera (default), champs=static 10-card crop, "
        "contain=letterbox whole card, cover=crop-fill 9:16",
    )
    p.add_argument(
        "--lobby-png",
        type=Path,
        default=None,
        help="Landscape lobby PNG for --still-mode story (default: <weave>_lobby.png)",
    )
    p.add_argument(
        "--lobby-meta",
        type=Path,
        default=None,
        help="Lobby sidecar JSON from generate_lobby_card (default: <png>_meta.json)",
    )
    p.add_argument(
        "--game-mode",
        choices=("crop", "fit"),
        default="crop",
        help="Gameplay pane: crop=zoomed center, fit=full HUD letterboxed",
    )
    p.add_argument(
        "--game-zoom",
        type=float,
        default=1.0,
        help="Crop zoom: 1.0=tight fill, 0.7/0.5=zoom out, 0=full-frame fit",
    )
    p.add_argument(
        "--cam-hole",
        choices=("black", "keep", "fill"),
        default="fill",
        help="When the 9:16 crop hits the source webcam: fill=blurred map (default), "
        "keep=leave the webcam, black=paint out",
    )
    p.add_argument(
        "--kda-overlay",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Layer source top-right KDA/CS/clock PIP onto gameplay (default on)",
    )
    p.add_argument("--kda-x", type=int, default=1316, help="KDA crop left in source")
    p.add_argument("--kda-y", type=int, default=4, help="KDA crop top in source")
    p.add_argument("--kda-w", type=int, default=520, help="KDA crop width in source")
    p.add_argument("--kda-h", type=int, default=32, help="KDA crop height in source")
    p.add_argument("--kda-out-w", type=int, default=560, help="KDA PIP width in portrait")
    p.add_argument("--kda-margin", type=int, default=0, help="KDA PIP margin from top-right (0 = flush)")
    p.add_argument("--start", type=float, default=None, help="Start seconds")
    p.add_argument("--duration", type=float, default=None, help="Duration seconds")
    p.add_argument("--crf", type=int, default=20)
    p.add_argument("--preset", default="veryfast")
    p.add_argument(
        "--preview-frame",
        type=Path,
        default=None,
        help="Also write a single JPEG of the portrait layout",
    )
    p.add_argument(
        "--track-champion",
        action="store_true",
        help="Pan the 9:16 gameplay crop to follow the local-player nameplate",
    )
    p.add_argument("--track-fps", type=float, default=4.0, help="Nameplate sample rate (default 4)")
    p.add_argument(
        "--track-dead-zone",
        type=float,
        default=0.10,
        help="Edge inset before the enemy counts as off the portrait (default 0.10)",
    )
    p.add_argument(
        "--track-ease-ms",
        type=float,
        default=280.0,
        help="Pan easing time constant in ms (default 280)",
    )
    p.add_argument(
        "--track-max-speed",
        type=float,
        default=860.0,
        help="Max horizontal crop pan speed in px/s (default 860)",
    )
    p.add_argument(
        "--track-self-bias",
        type=float,
        default=0.50,
        help="Blend toward self vs nearest enemy (0.5=mid, 1=self only)",
    )
    p.add_argument(
        "--track-enemy-pull",
        type=float,
        default=0.45,
        help="Max extra pull toward enemy as a fraction of crop width",
    )
    p.add_argument(
        "--track-pan-cooldown",
        type=float,
        default=0.7,
        help="Minimum seconds between pans (default 0.7)",
    )
    p.add_argument(
        "--track-outside-hold",
        type=float,
        default=0.12,
        help="Seconds the enemy must be off-screen before a pan",
    )
    p.add_argument(
        "--track-debug-dir",
        type=Path,
        default=None,
        help="Write annotated detection JPEGs for debugging",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.input.is_file():
        print(f"error: missing input {args.input}", file=sys.stderr)
        return 1
    try:
        result = render_portrait(
            args.input.resolve(),
            args.output.resolve(),
            cam_w=args.cam_w,
            cam_h=args.cam_h,
            cam_x=args.cam_x,
            cam_y=args.cam_y,
            out_w=args.width,
            out_h=args.height,
            cam_fraction=args.cam_fraction,
            still_seconds=float(args.still_seconds),
            still_mode=str(args.still_mode),
            lobby_png=args.lobby_png.resolve() if args.lobby_png else None,
            lobby_meta=args.lobby_meta.resolve() if args.lobby_meta else None,
            game_mode=str(args.game_mode),
            game_zoom=float(args.game_zoom),
            cam_hole=str(args.cam_hole),
            kda_overlay=bool(args.kda_overlay),
            kda_x=args.kda_x,
            kda_y=args.kda_y,
            kda_w=args.kda_w,
            kda_h=args.kda_h,
            kda_out_w=args.kda_out_w,
            kda_margin=args.kda_margin,
            start=args.start,
            duration=args.duration,
            crf=args.crf,
            preset=args.preset,
            preview_frame=args.preview_frame.resolve() if args.preview_frame else None,
            track_champion=bool(args.track_champion),
            track_fps=float(args.track_fps),
            track_dead_zone=float(args.track_dead_zone),
            track_ease_ms=float(args.track_ease_ms),
            track_max_speed=float(args.track_max_speed),
            track_self_bias=float(args.track_self_bias),
            track_enemy_pull=float(args.track_enemy_pull),
            track_pan_cooldown=float(args.track_pan_cooldown),
            track_outside_hold=float(args.track_outside_hold),
            track_debug_dir=args.track_debug_dir.resolve() if args.track_debug_dir else None,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
