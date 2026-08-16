#!/usr/bin/env python3
"""Cheap screen-space tracking of the local League champion.

Detects the self nameplate (gold/yellow HP fill + bright summoner-name text
above it) instead of running a full-frame detector. Missing frames (death,
dash, fog) hold the last known x. Camera motion uses a dead zone + easing
so the 9:16 crop follows like an editor pan, not a locked reticle.
Pans only when a nearby enemy leaves the portrait. The target is a
weighted point between you and them (self_bias 0.5 = midpoint,
lower = more enemy).
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from PIL import Image, ImageDraw


def even(n: int) -> int:
    return n - (n % 2)


def ffmpeg_escape_path(path: Path) -> str:
    """Escape a filesystem path for an FFmpeg filtergraph option."""
    text = str(path.resolve())
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", r"\'")


@dataclass
class Detection:
    t: float
    x: float
    y: float
    score: float
    enemy_x: float | None = None
    enemy_y: float | None = None


def _is_yellow_hp(r: int, g: int, b: int) -> bool:
    # Local-player HP fill on this overlay is gold/yellow, not ally-green.
    return r >= 150 and g >= 105 and b <= 125 and r >= b + 35 and g >= b + 15


def _is_green_hp(r: int, g: int, b: int) -> bool:
    return g >= 140 and g >= r + 30 and g >= b + 15 and r < 160 and b < 150


def _is_red_hp(r: int, g: int, b: int) -> bool:
    return r >= 160 and g <= 110 and b <= 110 and r >= g + 50 and r >= b + 50


def _is_name_text(r: int, g: int, b: int) -> bool:
    return r >= 165 and g >= 165 and b >= 135 and (r + g + b) >= 500


def _is_mana_blue(r: int, g: int, b: int) -> bool:
    return b >= 140 and b >= r + 25 and b >= g + 10


def _in_overlay(x: int, y: int, w: int, h: int) -> bool:
    # Stream overlay (rank / TRACK DIFF) and HUD / facecam.
    if y < int(0.14 * h):
        return True
    # Rank card + match-history row only. The old 30%×28% box ate
    # laner nameplates on the left of mid (Pantheon in the side bush).
    if x < int(0.26 * w) and y < int(0.22 * h):
        return True
    if y > int(0.76 * h):
        return True
    if x > int(0.80 * w) and y > int(0.60 * h):
        return True
    return False


def detect_nameplate(
    im: Image.Image,
    *,
    last_x: float | None = None,
    scale: float = 1.0,
) -> tuple[tuple[float, float, float] | None, tuple[float, float, float] | None]:
    """Return (self_hit, enemy_hit) as (x, y, score) tuples."""
    w, h = im.size
    px = im.load()
    y0, y1 = int(0.05 * h), int(0.76 * h)
    x0, x1 = int(0.02 * w), int(0.94 * w)
    min_run = max(6, int(16 * scale))
    max_run = max(min_run + 8, int(180 * scale))
    min_bright = max(18, int(110 * scale * scale))
    near = 240.0 * scale if last_x is not None else None

    def collect(kind: str, limit: int = 220) -> list[tuple[int, int, int]]:
        testers = {"yellow": _is_yellow_hp, "green": _is_green_hp, "red": _is_red_hp}
        test = testers[kind]
        runs: list[tuple[int, int, int]] = []
        for y in range(y0, y1):
            x = x0
            while x < x1:
                r, g, b = px[x, y]
                if not test(r, g, b):
                    x += 1
                    continue
                xs = x
                x += 1
                while x < x1:
                    r, g, b = px[x, y]
                    if not test(r, g, b):
                        break
                    x += 1
                if min_run <= (x - xs) <= max_run:
                    cx = (xs + x) // 2
                    if not _in_overlay(cx, y, w, h):
                        runs.append((xs, x, y))
                        if len(runs) >= limit:
                            return runs
        return runs

    def cluster(runs: list[tuple[int, int, int]]) -> list[dict[str, Any]]:
        if not runs:
            return []
        runs.sort(key=lambda t: (t[2], t[0]))
        y_slack = max(2, int(8 * scale))
        x_slack = max(8, int(24 * scale))
        groups: list[list[tuple[int, int, int]]] = [[runs[0]]]
        for run in runs[1:]:
            prev = groups[-1][-1]
            if run[2] - prev[2] <= y_slack and run[0] < prev[1] + x_slack and run[1] > prev[0] - x_slack:
                groups[-1].append(run)
            else:
                groups.append([run])
        min_n = 1 if scale < 0.8 else 2
        min_th = 1 if scale < 0.8 else max(2, int(2 * scale))
        bars: list[dict[str, Any]] = []
        for group in groups:
            if len(group) < min_n:
                continue
            ys = [g[2] for g in group]
            thick = max(ys) - min(ys) + 1
            if thick < min_th or thick > max(6, int(12 * scale)):
                continue
            xs = min(g[0] for g in group)
            xe = max(g[1] for g in group)
            cy = sum(ys) // len(group)
            cx = (xs + xe) // 2
            bars.append({"xs": xs, "xe": xe, "cx": cx, "cy": cy, "n": len(group)})
        bars.sort(key=lambda b: -b["n"])
        return bars[:12]

    def score_bar(bar: dict[str, Any], kind: str) -> tuple[float, float, float] | None:
        cy = int(bar["cy"])
        xs, xe = int(bar["xs"]), int(bar["xe"])
        ty0 = max(0, cy - max(8, int(28 * scale)))
        ty1 = max(0, cy - max(2, int(6 * scale)))
        tx0 = max(0, xs - max(10, int(36 * scale)))
        tx1 = min(w, xe + max(16, int(56 * scale)))
        bright = 0
        bsum = 0
        for ty in range(ty0, ty1):
            for tx in range(tx0, tx1):
                r, g, b = px[tx, ty]
                if _is_name_text(r, g, b):
                    bright += 1
                    bsum += tx
        if bright < min_bright:
            if kind != "red":
                return None
            # 720p enemy names are often too small; the red HP bar is enough.
            if (xe - xs) < max(12, int(18 * scale)):
                return None
            text_cx = float(bar["cx"])
            score = 18.0 + float(bar["n"]) * 6.0
            return (text_cx, float(cy), score)
        text_cx = bsum / bright
        # Mana bar sitting directly under HP is a strong self-nameplate cue.
        mana = 0
        my0 = cy + 1
        my1 = min(h, cy + max(3, int(8 * scale)))
        for my in range(my0, my1):
            for mx in range(xs, xe):
                r, g, b = px[mx, my]
                if _is_mana_blue(r, g, b):
                    mana += 1
        score = float(bright) + (80.0 if mana >= max(4, int(8 * scale)) else 0.0)
        if kind == "green":
            # Ally bars are also green; only keep if near last lock or very strong.
            score *= 0.55
            if last_x is not None and abs(text_cx - last_x) > (near or 0):
                return None
            if last_x is None and bright < min_bright * 1.8:
                return None
        if last_x is not None:
            dist = abs(text_cx - last_x)
            score += max(0.0, 60.0 - dist / max(scale, 0.01) / 8.0)
        return (text_cx, float(cy), score)

    ranked: list[tuple[float, float, float]] = []
    for kind in ("yellow", "green"):
        for bar in cluster(collect(kind)):
            hit = score_bar(bar, kind)
            if hit:
                ranked.append(hit)
        if ranked and kind == "yellow" and max(r[2] for r in ranked) >= 120:
            break
    if not ranked:
        return None, None
    ranked.sort(key=lambda t: -t[2])
    self_hit = ranked[0]
    enemy_hit = None
    reds: list[tuple[float, float, float]] = []
    for bar in cluster(collect("red")):
        hit = score_bar(bar, "red")
        if hit:
            reds.append(hit)
    if reds:
        sx, sy, _ = self_hit
        max_dy = 300.0 * max(scale, 0.5)
        nearby = [
            hit
            for hit in reds
            if abs(hit[1] - sy) <= max_dy
            and not _in_overlay(int(hit[0]), int(hit[1]), w, h)
        ]

        def fight_dist(hit: tuple[float, float, float]) -> float:
            return abs(hit[0] - sx) + 0.4 * abs(hit[1] - sy)

        if nearby:
            # A red bar glued to self is usually a minion / false lock,
            # not the laner sitting in a river bush.
            glued = 90.0 * max(scale, 0.5)
            far = [hit for hit in nearby if fight_dist(hit) >= glued]
            pool = far or nearby
            cand = min(pool, key=fight_dist)
            if fight_dist(cand) <= 820.0 * max(scale, 0.5):
                enemy_hit = cand
    return self_hit, enemy_hit


def iter_sample_frames(
    source: Path,
    *,
    start: float = 0.0,
    duration: float | None = None,
    fps: float = 4.0,
    width: int = 960,
    height: int = 540,
) -> Iterator[tuple[float, Image.Image]]:
    """Yield (time_in_segment, RGB frame) via ffmpeg rawvideo pipe."""
    vf = f"fps={fps:.3f},scale={width}:{height}:flags=fast_bilinear"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if start and start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(source)]
    if duration is not None and duration > 0:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert proc.stdout is not None
    frame_bytes = width * height * 3
    idx = 0
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            im = Image.frombytes("RGB", (width, height), buf)
            yield idx / max(fps, 0.001), im
            idx += 1
    finally:
        proc.stdout.close()
        proc.wait()


def sample_detections(
    source: Path,
    *,
    src_w: int,
    src_h: int,
    start: float = 0.0,
    duration: float | None = None,
    still_seconds: float = 0.0,
    fps: float = 4.0,
    debug_dir: Path | None = None,
) -> list[Detection | None]:
    """One slot per sample. None = no lock. Times are seconds into the play stream."""
    dw = min(960, even(max(2, src_w)))
    dh = even(max(2, int(round(src_h * dw / max(src_w, 1)))))
    sx = src_w / float(dw)
    sy = src_h / float(dh)
    still = max(0.0, float(still_seconds))
    last_x: float | None = None
    last_full: float | None = None
    out: list[Detection | None] = []
    debug_every = 12
    saved = 0

    print(
        f"[track] sampling {source.name} at {fps:.1f} fps "
        f"({dw}x{dh}, still={still:.2f}s)",
        flush=True,
    )
    for seg_t, im in iter_sample_frames(
        source, start=start, duration=duration, fps=fps, width=dw, height=dh
    ):
        play_t = seg_t - still
        if play_t < -1e-3:
            continue
        last_scaled = None if last_x is None else last_x / sx
        # Nameplates were calibrated at 1080p; shrink geometry on 720p weaves.
        plate_scale = max(0.45, dh / 1080.0)
        self_hit, enemy_hit = detect_nameplate(
            im, last_x=last_scaled, scale=plate_scale
        )
        if self_hit is None:
            out.append(None)
        else:
            x, y, score = self_hit
            det = Detection(
                t=play_t,
                x=x * sx,
                y=y * sy,
                score=score,
                enemy_x=(enemy_hit[0] * sx) if enemy_hit else None,
                enemy_y=(enemy_hit[1] * sy) if enemy_hit else None,
            )
            last_x = det.x
            last_full = det.x
            out.append(det)
            if debug_dir is not None and saved < 16 and (len(out) % debug_every == 1):
                debug_dir.mkdir(parents=True, exist_ok=True)
                vis = im.copy()
                dr = ImageDraw.Draw(vis)
                cx, cy = int(x), int(y)
                dr.line([(cx, 0), (cx, im.height)], fill=(0, 255, 255), width=2)
                dr.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], outline=(255, 255, 0), width=2)
                if enemy_hit:
                    ex, ey, _ = enemy_hit
                    dr.ellipse([int(ex) - 5, int(ey) - 5, int(ex) + 5, int(ey) + 5], outline=(255, 80, 80), width=2)
                    dr.line([(cx, cy), (int(ex), int(ey))], fill=(255, 180, 80), width=2)
                vis.save(debug_dir / f"det_{play_t:07.2f}.jpg", quality=80)
                saved += 1
        if len(out) % 50 == 0:
            locked = sum(1 for d in out if d is not None)
            print(
                f"[track] {play_t:6.1f}s  locked {locked}/{len(out)}  "
                f"x={last_full:.0f}" if last_full is not None else
                f"[track] {play_t:6.1f}s  locked {locked}/{len(out)}",
                flush=True,
            )
    locked = sum(1 for d in out if d is not None)
    print(f"[track] raw lock {locked}/{len(out)}", flush=True)
    return stabilize_detections(out, fps=fps)


def stabilize_detections(
    detections: list[Detection | None],
    *,
    fps: float,
    spike_px: float = 520.0,
) -> list[Detection | None]:
    """Drop one-sample x spikes (HUD gold, particles) while keeping real dashes."""
    if len(detections) < 3:
        return detections
    out = list(detections)
    for i in range(1, len(out) - 1):
        cur = out[i]
        if cur is None:
            continue
        prev = next((out[j] for j in range(i - 1, -1, -1) if out[j] is not None), None)
        nxt = next((out[j] for j in range(i + 1, len(out)) if out[j] is not None), None)
        if prev is None or nxt is None:
            continue
        dt_pn = max(1.0 / max(fps, 0.001), abs(nxt.t - prev.t))
        # Isolated jump that immediately returns is a false lock, not a dash.
        if (
            abs(cur.x - prev.x) > spike_px
            and abs(cur.x - nxt.x) > spike_px
            and abs(nxt.x - prev.x) < spike_px * 0.45
            and dt_pn < 1.1
        ):
            out[i] = None
    return out


def _in_crop(x: float, cam: float, crop_w: float, inset: float) -> bool:
    return (cam + inset) <= x <= (cam + crop_w - inset)


def camera_path(
    detections: list[Detection | None],
    *,
    crop_w: int,
    min_x: int,
    max_x: int,
    init_x: int,
    dead_zone: float = 0.10,
    ease_s: float = 0.28,
    fps: float = 4.0,
    max_speed_px_s: float = 860.0,
    self_bias: float = 0.50,
    enemy_pull: float = 0.0,
    pan_cooldown_s: float = 3.0,
    outside_hold_s: float = 1.5,
    reframe_frac: float = 0.10,
    teleport_px: float = 640.0,
) -> tuple[list[tuple[float, int]], int]:
    """Keep a nearby enemy in the 9:16 window without chasing every step.

    When both fit, the crop frames the pair with ``self_bias`` (1=center on
    you, 0.5=midpoint, 0=center on them) plus ``enemy_pull`` of leftover
    slack toward the enemy. When they don't fit, you sit near the edge
    looking at them.

    Also re-center when the desired crop has drifted by ``reframe_frac`` of
    the window (wide crops otherwise stay pointed at an old fight). A jump
    in self.x of ``teleport_px`` (stitched clip / recall) snaps after a few
    confirming samples. Walking or dashing off the crop eases at
    ``max_speed_px_s`` and still respects cooldown — it does not snap.
    When the enemy vanishes (brush / fog), keep peeking toward their last
    side so the bush stays in frame.
    """
    min_x = even(max(0, min_x))
    max_x = even(max(min_x, max_x))
    crop_w = even(max(2, crop_w))
    pad = max(10.0, float(dead_zone) * crop_w)
    edge = max(8.0, 0.08 * crop_w)

    def clamp_x(x: float) -> float:
        return max(min_x, min(x, max_x))

    def crop_for_pair(champ: float, enemy: float) -> float:
        left = min(champ, enemy) - pad
        right = max(champ, enemy) + pad
        bias = max(0.0, min(1.0, float(self_bias)))
        pull = max(0.0, min(1.0, float(enemy_pull)))
        if right - left <= crop_w:
            focus = champ * bias + enemy * (1.0 - bias)
            cam = focus - crop_w / 2.0
            lo = right - crop_w
            hi = left
            cam = max(lo, min(cam, hi))
            enemy_bound = hi if enemy >= champ else lo
            cam = cam + (enemy_bound - cam) * pull
            return clamp_x(cam)
        if enemy >= champ:
            return clamp_x(champ - edge)
        return clamp_x(champ - (crop_w - edge))

    look_frac = 0.12
    enemy_hold_miss = 8

    def crop_for_self(champ: float, look: float = 0.0) -> float:
        # look +1 = extra room on the right (peek into a river bush).
        cam = champ - crop_w / 2.0 + float(look) * look_frac * crop_w
        return clamp_x(cam)

    cam = float(clamp_x(init_x))
    first = next((d for d in detections if d is not None), None)
    last_look = 0.0
    if first is not None:
        if first.enemy_x is not None:
            last_look = 1.0 if first.enemy_x >= first.x else -1.0
            cam = crop_for_pair(first.x, first.enemy_x)
        else:
            cam = crop_for_self(first.x)
    target = cam
    held: float | None = None
    held_enemy: float | None = None
    enemy_miss = 0
    dt = 1.0 / max(fps, 0.001)
    alpha = 1.0 if ease_s <= 1e-3 else (1.0 - math.exp(-dt / ease_s))
    max_delta = max(8.0, float(max_speed_px_s) * dt)
    cooldown = max(0.25, float(pan_cooldown_s))
    hold_out = max(0.0, float(outside_hold_s))
    last_commit_t = -1e9
    outside_for = 0.0
    pan_commits = 0
    prev_x: float | None = None
    pending_jump_x: float | None = None
    pending_jump_n = 0
    reframe_px = max(48.0, float(reframe_frac) * crop_w)
    jump_px = max(120.0, float(teleport_px))
    path: list[tuple[float, int]] = []

    for i, det in enumerate(detections):
        t = i * dt if det is None else det.t
        teleported = False
        if det is not None:
            if prev_x is not None and abs(det.x - prev_x) >= jump_px:
                if pending_jump_x is not None and abs(det.x - pending_jump_x) < 90.0:
                    pending_jump_n += 1
                else:
                    pending_jump_x = det.x
                    pending_jump_n = 1
                if pending_jump_n >= 3:
                    teleported = True
                    prev_x = det.x
                    pending_jump_x = None
                    pending_jump_n = 0
            else:
                pending_jump_x = None
                pending_jump_n = 0
                prev_x = det.x
            held = det.x
            if det.enemy_x is not None:
                held_enemy = det.enemy_x
                last_look = 1.0 if det.enemy_x >= det.x else -1.0
                enemy_miss = 0
            else:
                enemy_miss += 1
                if enemy_miss >= enemy_hold_miss:
                    held_enemy = None
                    last_look = 0.0
        champ = held
        enemy = held_enemy
        # Hard cut only: stitched clip / recall. Walking off the 9:16
        # window used to snap here too, which bypassed cooldown + easing
        # and looked like an instant pan.
        if champ is not None and teleported:
            target = (
                crop_for_pair(champ, enemy)
                if enemy is not None
                else crop_for_self(champ, last_look)
            )
            cam = target
            last_commit_t = t
            outside_for = 0.0
            pan_commits += 1
            path.append((max(0.0, t), even(int(round(cam)))))
            continue
        if pending_jump_n >= 1:
            path.append((max(0.0, t), even(int(round(cam)))))
            continue
        want = target
        should_pan = False
        fully_out = False
        self_out = champ is not None and (champ < cam or champ > cam + crop_w)
        if champ is not None and enemy is not None:
            want = crop_for_pair(champ, enemy)
            enemy_in = _in_crop(enemy, cam, crop_w, pad)
            self_in = _in_crop(champ, cam, crop_w, 4.0)
            off = abs(want - cam)
            if (not enemy_in) or (not self_in):
                should_pan = True
                fully_out = (
                    self_out
                    or (enemy < cam)
                    or (enemy > cam + crop_w)
                )
            elif off >= reframe_px:
                should_pan = True
                fully_out = off >= reframe_px * 1.2
        elif champ is not None:
            want = crop_for_self(champ, last_look)
            off = abs(want - cam)
            if not _in_crop(champ, cam, crop_w, pad):
                should_pan = True
                fully_out = self_out
            elif off >= reframe_px:
                should_pan = True
                fully_out = off >= reframe_px * 1.2
        if should_pan:
            outside_for += dt
        elif abs(want - cam) < 80.0:
            outside_for = 0.0
        want = clamp_x(want)
        settled = abs(cam - target) < 8.0
        # Self off-screen: start sooner so we don't sit on empty map, but
        # still ease + cooldown. Reframe / enemy-peek keeps the long hold.
        need_hold = 0.35 if self_out or (should_pan and not fully_out) else hold_out
        if (
            should_pan
            and outside_for >= need_hold
            and (t - last_commit_t) >= cooldown
            and abs(want - cam) > 12.0
            and settled
        ):
            target = want
            last_commit_t = t
            outside_for = 0.0
            pan_commits += 1
        desired = cam * (1.0 - alpha) + target * alpha
        delta = desired - cam
        if abs(delta) > max_delta:
            desired = cam + math.copysign(max_delta, delta)
        cam = clamp_x(desired)
        path.append((max(0.0, t), even(int(round(cam)))))
    if not path:
        path.append((0.0, even(int(init_x))))
    return path, pan_commits


def densify_path(
    path: list[tuple[float, int]],
    *,
    fps: float = 30.0,
) -> list[tuple[float, int]]:
    """Linear-interpolate crop x, keeping only changes of ≥2px."""
    if len(path) <= 1:
        return path
    out: list[tuple[float, int]] = [path[0]]
    t0, t1 = path[0][0], path[-1][0]
    n = max(1, int(round((t1 - t0) * fps)))
    for i in range(1, n + 1):
        t = t0 + i * (t1 - t0) / n
        # binary-ish scan is overkill; path is small
        j = 1
        while j < len(path) and path[j][0] < t:
            j += 1
        a_t, a_x = path[j - 1]
        b_t, b_x = path[min(j, len(path) - 1)]
        if b_t <= a_t:
            x = a_x
        else:
            u = (t - a_t) / (b_t - a_t)
            x = int(round(a_x + (b_x - a_x) * u))
        x = even(x)
        if abs(x - out[-1][1]) >= 2:
            out.append((t, x))
    if out[-1][1] != path[-1][1]:
        out.append(path[-1])
    return out


def write_sendcmd(path: Path, keyframes: list[tuple[float, int]], *, filter_name: str = "crop@game") -> None:
    lines = [f"{t:.4f} {filter_name} x {x};" for t, x in keyframes]
    if not lines:
        lines.append(f"0.0 {filter_name} x 0;")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def track_for_portrait(
    source: Path,
    *,
    src_w: int,
    src_h: int,
    crop_w: int,
    min_x: int,
    max_x: int,
    init_x: int,
    start: float | None = None,
    duration: float | None = None,
    still_seconds: float = 0.0,
    fps: float = 4.0,
    dead_zone: float = 0.10,
    ease_s: float = 0.28,
    max_speed_px_s: float = 860.0,
    self_bias: float = 0.50,
    enemy_pull: float = 0.0,
    pan_cooldown_s: float = 3.0,
    outside_hold_s: float = 1.5,
    sendcmd_path: Path,
    dump_path: Path | None = None,
    debug_dir: Path | None = None,
) -> dict[str, Any]:
    dets = sample_detections(
        source,
        src_w=src_w,
        src_h=src_h,
        start=float(start or 0.0),
        duration=duration,
        still_seconds=still_seconds,
        fps=fps,
        debug_dir=debug_dir,
    )
    path, pan_commits = camera_path(
        dets,
        crop_w=crop_w,
        min_x=min_x,
        max_x=max_x,
        init_x=init_x,
        dead_zone=dead_zone,
        ease_s=ease_s,
        fps=fps,
        max_speed_px_s=max_speed_px_s,
        self_bias=self_bias,
        enemy_pull=enemy_pull,
        pan_cooldown_s=pan_cooldown_s,
        outside_hold_s=outside_hold_s,
        reframe_frac=0.10,
        teleport_px=640.0,
    )
    dense = densify_path(path, fps=30.0)
    write_sendcmd(sendcmd_path, dense)
    locked = [d for d in dets if d is not None]
    with_enemy = sum(1 for d in locked if d.enemy_x is not None)
    report = {
        "source": str(source),
        "samples": len(dets),
        "locked": len(locked),
        "lock_ratio": (len(locked) / len(dets)) if dets else 0.0,
        "enemy_ratio": (with_enemy / len(locked)) if locked else 0.0,
        "crop_w": crop_w,
        "min_x": min_x,
        "max_x": max_x,
        "init_x": init_x,
        "first_crop_x": dense[0][1] if dense else init_x,
        "dead_zone": dead_zone,
        "ease_s": ease_s,
        "max_speed_px_s": max_speed_px_s,
        "self_bias": self_bias,
        "enemy_pull": enemy_pull,
        "pan_cooldown_s": pan_cooldown_s,
        "outside_hold_s": outside_hold_s,
        "pan_commits": pan_commits,
        "fps": fps,
        "sendcmd": str(sendcmd_path),
        "sendcmd_keys": len(dense),
        "path": [{"t": round(t, 3), "x": x} for t, x in path],
        "detections": [
            {"t": round(d.t, 3), "x": round(d.x, 1), "y": round(d.y, 1), "score": round(d.score, 1),
             "enemy_x": None if d.enemy_x is None else round(d.enemy_x, 1)}
            for d in locked[:: max(1, len(locked) // 80)]
        ],
    }
    if dump_path is not None:
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        dump_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        f"[track] lock={report['lock_ratio']:.0%}  enemy={report['enemy_ratio']:.0%}  "
        f"pans={pan_commits}  crop_x {init_x}→ range "
        f"{min(x for _, x in path)}–{max(x for _, x in path)}  "
        f"keys={len(dense)} → {sendcmd_path.name}",
        flush=True,
    )
    return report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Track local-player nameplate for portrait crop pans.")
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--sendcmd", type=Path, required=True)
    p.add_argument("--dump", type=Path, default=None)
    p.add_argument("--debug-dir", type=Path, default=None)
    p.add_argument("--src-w", type=int, default=1920)
    p.add_argument("--src-h", type=int, default=1080)
    p.add_argument("--crop-w", type=int, default=888)
    p.add_argument("--min-x", type=int, default=0)
    p.add_argument("--max-x", type=int, default=448)
    p.add_argument("--init-x", type=int, default=448)
    p.add_argument("--start", type=float, default=0.0)
    p.add_argument("--duration", type=float, default=None)
    p.add_argument("--still-seconds", type=float, default=3.0)
    p.add_argument("--fps", type=float, default=4.0)
    p.add_argument("--dead-zone", type=float, default=0.10, help="Edge inset before enemy counts as off-screen")
    p.add_argument("--ease-ms", type=float, default=280.0)
    p.add_argument("--max-speed", type=float, default=860.0, help="Max crop pan speed in px/s")
    p.add_argument("--self-bias", type=float, default=0.50, help="1=center on you, 0.5=midpoint, 0=center on enemy")
    p.add_argument(
        "--enemy-pull",
        type=float,
        default=0.0,
        help="Extra shift toward the enemy as a fraction of leftover slack (0=off, 1=hard edge)",
    )
    p.add_argument("--pan-cooldown", type=float, default=3.0, help="Minimum seconds between pans")
    p.add_argument("--outside-hold", type=float, default=1.5, help="Seconds enemy must be off-screen before a pan")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.input.is_file():
        print(f"error: missing input {args.input}", file=sys.stderr)
        return 1
    track_for_portrait(
        args.input.resolve(),
        src_w=args.src_w,
        src_h=args.src_h,
        crop_w=args.crop_w,
        min_x=args.min_x,
        max_x=args.max_x,
        init_x=args.init_x,
        start=args.start,
        duration=args.duration,
        still_seconds=args.still_seconds,
        fps=args.fps,
        dead_zone=args.dead_zone,
        ease_s=max(0.05, float(args.ease_ms) / 1000.0),
        max_speed_px_s=float(args.max_speed),
        self_bias=float(args.self_bias),
        enemy_pull=float(args.enemy_pull),
        pan_cooldown_s=float(args.pan_cooldown),
        outside_hold_s=float(args.outside_hold),
        sendcmd_path=args.sendcmd.resolve(),
        dump_path=args.dump.resolve() if args.dump else None,
        debug_dir=args.debug_dir.resolve() if args.debug_dir else None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
