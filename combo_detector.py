#!/usr/bin/env python3
"""Detect LoL ability casts from HUD icon state changes and overlay the chain.

Crops Q/W/E/R/D/F from each frame and watches READY → COOLDOWN (or, for
LeBlanc W, READY → ACTIVE recast). Riot's Match API does not expose this.

    python combo_detector.py clip.mp4
    python combo_detector.py clip.mp4 --overlay -o clip_combo.mp4
    python combo_detector.py clip.mp4 --debug-dir data/_debug_combo/run
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from PIL import Image, ImageDraw, ImageFont

from ffmpeg_color import VIDEO_TO_BT709, X264_BT709
from generate_lobby_card import cached_bytes, ddragon_version, open_rgba


ROOT = Path(__file__).resolve().parent

# Calibrated on 1920x1080 landscape clips (portrait crops the HUD away).
# Values are (x, y, size) at 1080p, 100% HUD scale.
SLOTS_1080: dict[str, tuple[int, int, int]] = {
    "Q": (764, 956, 48),
    "W": (820, 956, 48),
    "E": (880, 948, 52),
    "R": (948, 948, 56),
    "D": (1040, 954, 36),
    "F": (1088, 954, 36),
}

# R first so R(W)+E on the same frame lists as R > E.
CAST_ORDER = ("R", "Q", "W", "E", "D", "F")

# Ignore re-triggers while the icon is still on cooldown. Recast (W↩) is
# handled separately — this window used to swallow the return click.
REFRACTORY_S: dict[str, float] = {
    "Q": 2.2,
    "W": 3.4,
    "E": 4.0,
    "R": 8.0,
    "D": 20.0,
    "F": 20.0,
}

# Distortion return is ACTIVE → COOLDOWN. The unused 4s pad falling off
# looks the same, so ignore drops that land near the window end.
RECAST_MIN_S = 0.15
RECAST_EXPIRE_S = 3.65

# Flash icon goes CD immediately; E (and other basics) lag a frame or two.
# Treat near-simultaneous Flash + ability as ability → Flash (E-flash).
ABILITY_FLASH_S = 0.35

# Real LB ult stays on CD for tens of seconds. If the icon is READY again
# this soon, the READY → COOLDOWN blip was HUD flicker, not a cast.
R_MIN_CD_S = 8.0

HUD_PAD = 12

CHAMPIONS: dict[str, dict[str, Any]] = {
    "leblanc": {
        "spells": {
            "Q": "LeblancQ",
            "W": "LeblancW",
            "E": "LeblancE",
            "R": "LeblancR",
        },
        "recast": {"W"},
        "mimic_r": True,
        "names": {
            "Q": "Q",
            "W": "W",
            "E": "E",
            "R": "R",
            "W2": "W↩",
        },
    },
}

SUMMONER_SPELLS: dict[str, tuple[str, str]] = {
    "FLASH": ("SummonerFlash", "FLASH"),
    "IGNITE": ("SummonerDot", "IGNITE"),
    "TELEPORT": ("SummonerTeleport", "TP"),
    "EXHAUST": ("SummonerExhaust", "EXHAUST"),
    "GHOST": ("SummonerHaste", "GHOST"),
    "CLEANSE": ("SummonerBoost", "CLEANSE"),
    "BARRIER": ("SummonerBarrier", "BARRIER"),
    "HEAL": ("SummonerHeal", "HEAL"),
    "SMITE": ("SummonerSmite", "SMITE"),
}

BASIC = ("Q", "W", "E")
READY, ACTIVE, COOLDOWN = "ready", "active", "cooldown"


def _first_existing(*candidates: str) -> str:
    for path in candidates:
        if path and Path(path).is_file():
            return path
    return candidates[-1]


FONT_BOLD = _first_existing(
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
FONT_REG = _first_existing(
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def font(path: str, size: int):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def even(n: int) -> int:
    return n - (n % 2)


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
            "stream=width,height",
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
    return {
        "width": int(stream.get("width") or 1920),
        "height": int(stream.get("height") or 1080),
        "duration": duration,
    }


def scaled_slots(
    src_w: int,
    src_h: int,
    *,
    shift_x: int = 0,
    shift_y: int = 0,
) -> dict[str, tuple[int, int, int]]:
    sx = src_w / 1920.0
    sy = src_h / 1080.0
    out: dict[str, tuple[int, int, int]] = {}
    for name, (x, y, size) in SLOTS_1080.items():
        out[name] = (
            int(round(x * sx)) + shift_x,
            int(round(y * sy)) + shift_y,
            max(8, int(round(size * min(sx, sy)))),
        )
    return out


def hud_crop_box(slots: dict[str, tuple[int, int, int]]) -> tuple[int, int, int, int]:
    xs = [v[0] for v in slots.values()]
    ys = [v[1] for v in slots.values()]
    rights = [v[0] + v[2] for v in slots.values()]
    bottoms = [v[1] + v[2] for v in slots.values()]
    x0 = even(max(0, min(xs) - HUD_PAD))
    y0 = even(max(0, min(ys) - HUD_PAD))
    x1 = even(max(rights) + HUD_PAD + 1)
    y1 = even(max(bottoms) + HUD_PAD + 1)
    return x0, y0, x1 - x0, y1 - y0


def local_slots(
    slots: dict[str, tuple[int, int, int]],
    origin: tuple[int, int],
) -> dict[str, tuple[int, int, int]]:
    ox, oy = origin
    return {k: (x - ox, y - oy, s) for k, (x, y, s) in slots.items()}


def iter_hud_frames(
    source: Path,
    *,
    crop: tuple[int, int, int, int],
    fps: float,
    start: float = 0.0,
    duration: float | None = None,
) -> Iterator[tuple[float, Image.Image]]:
    x, y, w, h = crop
    vf = f"fps={fps:.3f},crop={w}:{h}:{x}:{y}"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(source)]
    if duration is not None and duration > 0:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert proc.stdout is not None
    frame_bytes = w * h * 3
    idx = 0
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            yield idx / max(fps, 0.001), Image.frombytes("RGB", (w, h), buf)
            idx += 1
    finally:
        proc.stdout.close()
        proc.wait()


def _is_white(r: int, g: int, b: int) -> bool:
    return r >= 210 and g >= 210 and b >= 210 and abs(r - g) < 25 and abs(g - b) < 25


def _is_gold(r: int, g: int, b: int) -> bool:
    return r >= 170 and g >= 130 and b <= 110 and r >= g and g >= b + 25


def slot_metrics(im: Image.Image, box: tuple[int, int, int]) -> dict[str, float]:
    x, y, size = box
    w, h = im.size
    x0 = max(0, x)
    y0 = max(0, y)
    x1 = min(w, x + size)
    y1 = min(h, y + size)
    if x1 <= x0 or y1 <= y0:
        return {
            "luma": 0.0,
            "sat": 0.0,
            "white": 0.0,
            "gold": 0.0,
            "mean_r": 0.0,
            "mean_g": 0.0,
            "mean_b": 0.0,
            "blue": 0.0,
            "redness": 0.0,
        }

    inset = max(3, size // 6)
    ix0, iy0 = x0 + inset, y0 + inset
    ix1, iy1 = x1 - inset, y1 - inset
    inner = im.crop((ix0, iy0, max(ix0 + 1, ix1), max(iy0 + 1, iy1)))
    pixels = list(inner.getdata())
    n = max(1, len(pixels))
    luma = 0.0
    sat = 0.0
    white = 0
    sr = sg = sb = 0.0
    for r, g, b in pixels:
        luma += 0.299 * r + 0.587 * g + 0.114 * b
        sat += max(r, g, b) - min(r, g, b)
        sr += r
        sg += g
        sb += b
        if _is_white(r, g, b):
            white += 1
    mean_r, mean_g, mean_b = sr / n, sg / n, sb / n

    ring = 0
    gold = 0
    px = im.load()
    ring_w = max(2, size // 10)
    for yy in range(y0, y1):
        for xx in range(x0, x1):
            on_edge = (
                xx < x0 + ring_w
                or xx >= x1 - ring_w
                or yy < y0 + ring_w
                or yy >= y1 - ring_w
            )
            if not on_edge:
                continue
            r, g, b = px[xx, yy]
            ring += 1
            if _is_gold(r, g, b):
                gold += 1

    return {
        "luma": luma / n,
        "sat": sat / n,
        "white": white / n,
        "gold": gold / max(1, ring),
        "mean_r": mean_r,
        "mean_g": mean_g,
        "mean_b": mean_b,
        "blue": float(
            mean_b > mean_r + 15.0 and mean_b >= mean_g - 8.0 and mean_r < 90.0
        ),
        "redness": mean_r - mean_g,
    }


def is_mimic_w(m: dict[str, float]) -> bool:
    """LeBlanc R showing Distortion: red/pink W icon, not the default purple R."""
    return m.get("mean_r", 0) >= 140.0 and m.get("redness", 0) >= 80.0


def classify_state(
    m: dict[str, float],
    *,
    slot: str,
    recast: bool,
    prev: str | None = None,
) -> str:
    # Blue CD overlay is more reliable than timer digits (E's "13" is faint).
    enter, leave = 0.032, 0.018
    on_cd = bool(m.get("blue")) or m["white"] >= (leave if prev == COOLDOWN else enter)
    if on_cd:
        return COOLDOWN
    # Scoreboard / VFX / death cam can dim the HUD. That is not READY.
    if m["luma"] < 42.0:
        return prev or COOLDOWN
    # Mimic:Distortion stays bright until the 4s return window ends.
    if slot == "R" and is_mimic_w(m):
        return ACTIVE
    # Distortion recast: icon stays colorful, gold frame rises, mana cost drops.
    # Ready W is gold~0.02 sat~33; recast is gold~0.08 sat~62.
    if recast and m["gold"] >= 0.05 and m["sat"] >= 48.0:
        return ACTIVE
    return READY


@dataclass
class CastEvent:
    t: float
    slot: str
    spell: str
    label: str
    recast: bool = False
    mimic: str | None = None

    def as_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "t": round(self.t, 3),
            "slot": self.slot,
            "spell": self.spell,
            "label": self.label,
        }
        if self.recast:
            row["recast"] = True
        if self.mimic:
            row["mimic"] = self.mimic
        return row


@dataclass
class Combo:
    events: list[CastEvent] = field(default_factory=list)

    @property
    def t0(self) -> float:
        return self.events[0].t if self.events else 0.0

    @property
    def t1(self) -> float:
        return self.events[-1].t if self.events else 0.0

    def chain(self) -> list[str]:
        return [e.label for e in self.events]

    def as_dict(self) -> dict[str, Any]:
        return {
            "t0": round(self.t0, 3),
            "t1": round(self.t1, 3),
            "n": len(self.events),
            "chain": self.chain(),
            "events": [e.as_dict() for e in self.events],
        }


def parse_summoners(text: str) -> dict[str, str]:
    parts = [p.strip().upper() for p in (text or "").split(",") if p.strip()]
    if len(parts) != 2:
        raise ValueError("--summoners must be two names, e.g. FLASH,IGNITE")
    for name in parts:
        if name not in SUMMONER_SPELLS:
            raise ValueError(f"unknown summoner {name!r}; {', '.join(SUMMONER_SPELLS)}")
    return {"D": parts[0], "F": parts[1]}


def infer_mimic(m: dict[str, float], last_basic: str | None) -> str | None:
    if is_mimic_w(m):
        return "W"
    if m.get("mean_r", 0) > 140 and m.get("mean_g", 0) > 110 and m.get("mean_b", 0) < 110:
        return "Q"
    # W mimic always goes ACTIVE (recast glow). Don't inherit W from a
    # previous Distortion when this R press is actually E/Q.
    if last_basic == "W":
        return None
    return last_basic


def _restore_r_memory(events: list[CastEvent]) -> tuple[float, str | None]:
    last_t = -99.0
    mimic: str | None = None
    for ev in reversed(events):
        if ev.slot != "R":
            continue
        last_t = ev.t
        if not ev.recast:
            mimic = ev.mimic
        break
    return last_t, mimic


def retract_false_ult(
    events: list[CastEvent],
    *,
    t: float,
    last_fire: dict[str, float],
) -> str | None:
    """Drop an R cast whose icon is READY again too soon to be a real ult."""
    for idx in range(len(events) - 1, -1, -1):
        ev = events[idx]
        if ev.slot != "R":
            continue
        if ev.recast:
            return None
        if t - ev.t >= R_MIN_CD_S:
            return None
        events.pop(idx)
        last_fire["R"], mimic = _restore_r_memory(events)
        print(f"[cast] {ev.t:6.2f}s  {ev.label} retracted (icon ready again)", flush=True)
        return mimic
    return None


def prefer_ability_flash(events: list[CastEvent]) -> list[CastEvent]:
    """HUD shows Flash CD before E; display as E → Flash when they overlap."""
    for i, flash in enumerate(events):
        if flash.spell != "FLASH":
            continue
        for other in events:
            if other is flash or other.recast:
                continue
            if other.slot not in (*BASIC, "R"):
                continue
            dt = other.t - flash.t
            if 0 <= dt <= ABILITY_FLASH_S:
                t0 = min(flash.t, other.t)
                other.t = t0
                flash.t = round(t0 + 0.05, 3)
                break
    order = {name: i for i, name in enumerate(CAST_ORDER)}
    events.sort(key=lambda e: (e.t, order.get(e.slot, 99)))
    return events


def spell_label(
    slot: str,
    *,
    champion: dict[str, Any],
    summoners: dict[str, str],
    last_basic: str | None,
    recast: bool,
    metrics: dict[str, float] | None = None,
    r_mimic: str | None = None,
) -> tuple[str, str, str | None]:
    """Return (spell_key, display_label, mimic)."""
    if slot in ("D", "F"):
        key = summoners[slot]
        return key, SUMMONER_SPELLS[key][1], None
    if recast and slot in champion.get("recast", set()):
        return f"{slot}2", champion.get("names", {}).get("W2", f"{slot}↩"), None
    if recast and slot == "R":
        # Return pad is whatever R copied on the dash, not the last basic
        # (E between R(W) and R↩ must not turn this into R(E)).
        mimic = r_mimic or infer_mimic(metrics or {}, None) or "W"
        return "R2", f"R({mimic})↩", mimic
    if slot == "R" and champion.get("mimic_r"):
        mimic = infer_mimic(metrics or {}, last_basic)
        if mimic:
            return "R", f"R({mimic})", mimic
        return "R", "R", None
    return slot, champion.get("names", {}).get(slot, slot), None


def detect_casts(
    source: Path,
    *,
    fps: float = 20.0,
    champion_id: str = "leblanc",
    summoners: dict[str, str] | None = None,
    gap: float = 1.8,
    include_recast: bool = True,
    shift_x: int = 0,
    shift_y: int = 0,
    start: float = 0.0,
    duration: float | None = None,
    debug_dir: Path | None = None,
) -> dict[str, Any]:
    champ_id = champion_id.strip().lower()
    if champ_id not in CHAMPIONS:
        raise ValueError(f"unsupported champion {champion_id!r}; {', '.join(CHAMPIONS)}")
    champ = CHAMPIONS[champ_id]
    recast_slots = set(champ.get("recast") or ())
    summons = summoners or {"D": "FLASH", "F": "IGNITE"}

    info = probe(source)
    slots_full = scaled_slots(info["width"], info["height"], shift_x=shift_x, shift_y=shift_y)
    crop = hud_crop_box(slots_full)
    slots = local_slots(slots_full, (crop[0], crop[1]))

    print(
        f"[combo] {source.name}  {info['width']}x{info['height']}  "
        f"hud={crop[2]}x{crop[3]}@{crop[0]},{crop[1]}  fps={fps:.1f}",
        flush=True,
    )

    prev_state: dict[str, str] = {}
    last_fire: dict[str, float] = {k: -99.0 for k in slots}
    last_basic: str | None = None
    last_r_mimic: str | None = None
    events: list[CastEvent] = []
    debug_rows: list[dict[str, Any]] = []
    active_run: dict[str, int] = {k: 0 for k in slots}
    ready_run: dict[str, int] = {k: 0 for k in slots}
    dumped = 0
    warmup = max(3, int(round(fps * 0.15)))
    ready_need = max(6, int(round(fps * 0.3)))
    frame_i = 0

    if debug_dir is not None:
        debug_dir.mkdir(parents=True, exist_ok=True)

    for t, im in iter_hud_frames(
        source, crop=crop, fps=fps, start=start, duration=duration
    ):
        row: dict[str, Any] = {"t": round(t, 3)}
        metrics_by: dict[str, dict[str, float]] = {}
        states: dict[str, str] = {}
        for name, box in slots.items():
            metrics_by[name] = slot_metrics(im, box)
            states[name] = classify_state(
                metrics_by[name],
                slot=name,
                recast=name in recast_slots,
                prev=prev_state.get(name),
            )

        r_mimic_w = states.get("R") == ACTIVE and is_mimic_w(metrics_by.get("R") or {})

        for name in CAST_ORDER:
            if name not in slots:
                continue
            box = slots[name]
            metrics = metrics_by[name]
            state = states[name]
            row[name] = {"state": state, **{k: round(float(v), 3) for k, v in metrics.items()}}

            if name not in prev_state:
                prev_state[name] = state
                continue

            if state == ACTIVE:
                active_run[name] += 1
            else:
                active_run[name] = 0
            if state == READY:
                ready_run[name] += 1
            else:
                ready_run[name] = 0

            prev = prev_state[name]
            if name == "R" and ready_run[name] == ready_need:
                restored = retract_false_ult(events, t=t, last_fire=last_fire)
                if restored is not None or last_fire.get("R", -99.0) < 0:
                    last_r_mimic = restored
            is_recast = False
            fired = False
            refractory = REFRACTORY_S.get(name, 1.0)
            cooled = t - last_fire[name] >= refractory
            recast_end = (
                prev == ACTIVE
                and state == COOLDOWN
                and (name in recast_slots or name == "R")
            )
            if frame_i >= warmup:
                if recast_end:
                    recast_s = t - last_fire[name]
                    # Real return click, not the unused pad expiring (~4s).
                    if include_recast and RECAST_MIN_S <= recast_s < RECAST_EXPIRE_S:
                        fired = True
                        is_recast = True
                elif cooled and prev in {READY, ACTIVE} and state == COOLDOWN:
                    # Don't treat W CD as a dash while R is showing mimic W.
                    if not (name == "W" and r_mimic_w):
                        fired = True
                elif cooled and prev == READY and state == ACTIVE:
                    # W dash / R(W) dash: the recast glow is the cast, not the later CD.
                    fired = True
            if fired:
                spell, label, mimic = spell_label(
                    name,
                    champion=champ,
                    summoners=summons,
                    last_basic=last_basic,
                    recast=is_recast,
                    metrics=metrics,
                    r_mimic=last_r_mimic,
                )
                events.append(
                    CastEvent(
                        t=t,
                        slot=name,
                        spell=spell,
                        label=label,
                        recast=is_recast,
                        mimic=mimic,
                    )
                )
                last_fire[name] = t
                if name in BASIC:
                    last_basic = name
                if name == "R" and not is_recast:
                    last_r_mimic = mimic
                print(f"[cast] {t:6.2f}s  {label}", flush=True)
                if debug_dir is not None and dumped < 24:
                    vis = im.copy()
                    dr = ImageDraw.Draw(vis)
                    bx, by, bs = box
                    dr.rectangle([bx, by, bx + bs, by + bs], outline=(0, 255, 80), width=2)
                    safe = label.replace("(", "").replace(")", "").replace(">", "")
                    vis.save(debug_dir / f"cast_{t:07.3f}_{safe}.png")
                    dumped += 1

            prev_state[name] = state
        debug_rows.append(row)
        frame_i += 1

    events = prefer_ability_flash(events)
    combos = group_combos(events, gap=gap)
    payload = {
        "source": str(source),
        "champion": champ_id,
        "summoners": summons,
        "fps": fps,
        "width": info["width"],
        "height": info["height"],
        "duration": round(info["duration"], 3),
        "slots_1080": SLOTS_1080,
        "crop": {"x": crop[0], "y": crop[1], "w": crop[2], "h": crop[3]},
        "events": [e.as_dict() for e in events],
        "combos": [c.as_dict() for c in combos],
    }
    if debug_dir is not None:
        (debug_dir / "metrics.jsonl").write_text(
            "\n".join(json.dumps(r) for r in debug_rows) + "\n",
            encoding="utf-8",
        )
        (debug_dir / "combo.json").write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )
        save_slot_overlay(source, slots_full, debug_dir / "slots.png")
    return payload


def _recast_extends(combo: Combo, event: CastEvent) -> bool:
    """Keep W↩ / R↩ on the dash combo even if the return is after `gap`."""
    if not event.recast or not combo.events:
        return False
    dashes = [e for e in combo.events if e.slot == event.slot and not e.recast]
    if not dashes:
        return False
    return event.t - dashes[-1].t < RECAST_EXPIRE_S


def group_combos(events: list[CastEvent], *, gap: float) -> list[Combo]:
    combos: list[Combo] = []
    cur = Combo()
    for event in events:
        if cur.events and event.t - cur.t1 > gap and not _recast_extends(cur, event):
            combos.append(cur)
            cur = Combo()
        cur.events.append(event)
    if cur.events:
        combos.append(cur)
    return combos


def save_slot_overlay(
    source: Path,
    slots: dict[str, tuple[int, int, int]],
    dest: Path,
    *,
    t: float = 1.0,
) -> None:
    tmp = dest.with_suffix(".full.png")
    run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{t:.3f}",
            "-i",
            str(source),
            "-frames:v",
            "1",
            str(tmp),
        ]
    )
    im = Image.open(tmp).convert("RGB")
    dr = ImageDraw.Draw(im)
    for name, (x, y, size) in slots.items():
        dr.rectangle([x, y, x + size, y + size], outline=(0, 255, 80), width=2)
        dr.text((x, max(0, y - 14)), name, fill=(0, 255, 80), font=font(FONT_BOLD, 14))
    xs = [v[0] for v in slots.values()]
    ys = [v[1] for v in slots.values()]
    rights = [v[0] + v[2] for v in slots.values()]
    bottoms = [v[1] + v[2] for v in slots.values()]
    im.crop((min(xs) - 20, min(ys) - 24, max(rights) + 20, max(bottoms) + 20)).save(dest)
    tmp.unlink(missing_ok=True)


def spell_icon(spell_id: str) -> Image.Image | None:
    ver = ddragon_version()
    data = cached_bytes(
        f"https://ddragon.leagueoflegends.com/cdn/{ver}/img/spell/{spell_id}.png"
    )
    if not data:
        return None
    return open_rgba(data)


def icon_for_event(event: dict[str, Any], champion: dict[str, Any]) -> Image.Image:
    slot = str(event.get("slot") or "")
    mimic = event.get("mimic")
    spell = str(event.get("spell") or slot)
    champ_spells: dict[str, str] = champion.get("spells") or {}
    if slot in ("D", "F") or spell in SUMMONER_SPELLS:
        key = spell if spell in SUMMONER_SPELLS else None
        sid = SUMMONER_SPELLS[key][0] if key else None
    elif slot == "R" and mimic and mimic in champ_spells:
        sid = champ_spells[str(mimic)]
    elif slot in champ_spells:
        sid = champ_spells[slot]
    elif spell.rstrip("2") in champ_spells:
        sid = champ_spells[spell.rstrip("2")]
    else:
        sid = None
    icon = spell_icon(sid) if sid else None
    if icon is None:
        icon = Image.new("RGBA", (64, 64), (40, 44, 56, 255))
        dr = ImageDraw.Draw(icon)
        dr.text((8, 18), str(event.get("label") or slot), fill=(255, 220, 90), font=font(FONT_BOLD, 22))
    return icon


def rounded_icon(icon: Image.Image, size: int, *, mimic: bool = False) -> Image.Image:
    icon = icon.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=max(6, size // 8), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(icon, (0, 0), mask)
    dr = ImageDraw.Draw(out)
    border = (196, 92, 255, 255) if mimic else (255, 214, 90, 255)
    dr.rounded_rectangle([1, 1, size - 2, size - 2], radius=max(6, size // 8), outline=border, width=3)
    return out


def make_spell_tile(
    event: dict[str, Any],
    champion: dict[str, Any],
    *,
    icon_size: int,
    cell_w: int,
    cell_h: int,
    show_chevron: bool,
    label_px: int | None = None,
    gap: int = 6,
) -> Image.Image:
    tile = Image.new("RGBA", (cell_w, cell_h), (0, 0, 0, 0))
    dr = ImageDraw.Draw(tile)
    icon = rounded_icon(
        icon_for_event(event, champion),
        icon_size,
        mimic=bool(event.get("mimic")),
    )
    label_px = int(label_px) if label_px is not None else max(13, icon_size // 5)
    content_h = icon_size + gap + label_px
    iy = max(0, (cell_h - content_h) // 2)
    chev_px = max(22, icon_size // 3) if show_chevron else 0
    chev_w = (chev_px + 10) if show_chevron else 0
    ix = chev_w + max(0, (cell_w - chev_w - icon_size) // 2)
    if show_chevron:
        chev = font(FONT_BOLD, chev_px)
        dr.text(
            (4, iy + icon_size // 2 - chev_px // 2),
            ">",
            fill=(255, 214, 90, 255),
            font=chev,
        )
    # Dark plate behind the icon.
    plate = Image.new("RGBA", (icon_size + 8, icon_size + 8), (0, 0, 0, 0))
    ImageDraw.Draw(plate).rounded_rectangle(
        [0, 0, icon_size + 7, icon_size + 7],
        radius=max(10, icon_size // 12),
        fill=(8, 10, 16, 210),
    )
    tile.alpha_composite(plate, (ix - 4, iy - 4))
    tile.alpha_composite(icon, (ix, iy))
    label = str(event.get("label") or event.get("slot") or "")
    lf = font(FONT_BOLD, label_px)
    bbox = dr.textbbox((0, 0), label, font=lf)
    tw = bbox[2] - bbox[0]
    dr.text(
        (ix + (icon_size - tw) // 2, iy + icon_size + gap),
        label,
        fill=(244, 244, 248, 255),
        font=lf,
    )
    return tile


def _band_icon_metrics(band_h: int, fill: float) -> tuple[int, int, int]:
    """Icon, label, and gap so icon+text is `fill` of the band height."""
    content_h = max(24, int(round(band_h * fill)))
    gap = max(4, int(round(content_h * 0.05)))
    label_px = max(14, int(round(content_h * 0.18)))
    icon_size = max(16, content_h - gap - label_px)
    return icon_size, label_px, gap


def prepare_overlay_tiles(
    payload: dict[str, Any],
    dest_dir: Path,
    *,
    src_w: int,
    src_h: int,
    hold: float = 2.2,
    y_frac: float = 0.08,
    y: int | None = None,
    icon_size: int | None = None,
    band_y: int | None = None,
    band_h: int | None = None,
    fill: float = 0.8,
) -> list[dict[str, Any]]:
    """Write combo PNG tiles. Returns [{path, x, y, t0, t1}, ...]."""
    champ = CHAMPIONS[str(payload.get("champion") or "leblanc")]
    combos = payload.get("combos") or []
    if not combos:
        return []

    label_px: int | None = None
    gap = 6
    if band_h is not None:
        icon_size, label_px, gap = _band_icon_metrics(int(band_h), float(fill))
        cell_h = int(band_h)
        y = int(band_y) if band_y is not None else int(y if y is not None else src_h * y_frac)
    else:
        if icon_size is None:
            icon_size = 72 if src_w >= 1600 else 56
        cell_h = icon_size + max(36, icon_size // 4 + 16)
        y = int(y) if y is not None else int(src_h * y_frac)
    chev_px = max(22, icon_size // 3)
    cell_icon_w = icon_size + 12
    cell_chev_w = chev_px + 10 + cell_icon_w

    dest_dir.mkdir(parents=True, exist_ok=True)
    tiles: list[dict[str, Any]] = []
    idx = 0
    for combo in combos:
        events = combo.get("events") or []
        if len(events) < 2:
            continue
        n = len(events)
        widths = [cell_icon_w if i == 0 else cell_chev_w for i in range(n)]
        total_w = sum(widths)
        x0 = max(8, (src_w - total_w) // 2)
        t_end = float(combo["t1"]) + hold
        x = x0
        for i, event in enumerate(events):
            tile = make_spell_tile(
                event,
                champ,
                icon_size=icon_size,
                cell_w=widths[i],
                cell_h=cell_h,
                show_chevron=i > 0,
                label_px=label_px,
                gap=gap,
            )
            path = dest_dir / f"tile_{idx:03d}.png"
            tile.save(path)
            tiles.append(
                {
                    "path": path,
                    "x": x,
                    "y": y,
                    "t0": float(event["t"]),
                    "t1": t_end,
                }
            )
            x += widths[i]
            idx += 1
    return tiles


def render_overlay(
    source: Path,
    payload: dict[str, Any],
    dest: Path,
    *,
    hold: float = 2.2,
    y_frac: float = 0.08,
    y: int | None = None,
    icon_size: int | None = None,
    band_y: int | None = None,
    band_h: int | None = None,
    fill: float = 0.8,
) -> None:
    combos = payload.get("combos") or []
    if not combos:
        raise RuntimeError("no combos to overlay")

    info = probe(source)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="combo_ov_") as raw:
        tiles = prepare_overlay_tiles(
            payload,
            Path(raw),
            src_w=info["width"],
            src_h=info["height"],
            hold=hold,
            y_frac=y_frac,
            y=y,
            icon_size=icon_size,
            band_y=band_y,
            band_h=band_h,
            fill=fill,
        )
        if not tiles:
            raise RuntimeError("no combo with 2+ spells to overlay")

        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source)]
        for tile in tiles:
            cmd += ["-loop", "1", "-t", f"{info['duration']:.3f}", "-i", str(tile["path"])]

        parts: list[str] = []
        last = "[0:v]"
        for i, tile in enumerate(tiles):
            src = i + 1
            tag = f"s{i}"
            t0 = float(tile["t0"])
            t1 = float(tile["t1"])
            parts.append(
                f"[{src}:v]format=rgba,fade=t=in:st={t0:.3f}:d=0.08:alpha=1,"
                f"fade=t=out:st={max(t0, t1 - 0.35):.3f}:d=0.35:alpha=1[ov{i}];"
                f"{last}[ov{i}]overlay=x={int(tile['x'])}:y={int(tile['y'])}:enable='between(t,{t0:.3f},{t1:.3f})'[{tag}];"
            )
            last = f"[{tag}]"
        parts.append(f"{last}{VIDEO_TO_BT709}[v]")
        filt = "".join(parts)

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
            "veryfast",
            "-crf",
            "18",
            *X264_BT709,
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(dest),
        ]
        run(cmd)
    print(f"[combo] overlay → {dest}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Detect LoL ability combos from HUD icon cooldowns."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("-o", "--output", type=Path, help="Overlay mp4 path (implies --overlay)")
    parser.add_argument("--json", type=Path, help="Write combo.json here")
    parser.add_argument("--overlay", action="store_true", help="Burn the icon chain onto the clip")
    parser.add_argument("--champion", default="leblanc")
    parser.add_argument("--summoners", default="FLASH,IGNITE", help="D,F mapping")
    parser.add_argument("--fps", type=float, default=20.0)
    parser.add_argument("--gap", type=float, default=1.8, help="Seconds between combos")
    parser.add_argument(
        "--no-recast",
        action="store_true",
        help="Ignore LeBlanc W/R return (default: count it as W↩ / R↩)",
    )
    parser.add_argument("--shift-x", type=int, default=0)
    parser.add_argument("--shift-y", type=int, default=0)
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--duration", type=float, default=0.0)
    parser.add_argument("--hold", type=float, default=2.2, help="Seconds the chain stays after the last hit")
    parser.add_argument("--debug-dir", type=Path)
    parser.add_argument("--calibrate", action="store_true", help="Dump HUD slot overlay and exit")
    args = parser.parse_args()

    source = args.input.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)

    if args.calibrate:
        info = probe(source)
        slots = scaled_slots(info["width"], info["height"], shift_x=args.shift_x, shift_y=args.shift_y)
        dest = args.debug_dir or source.with_name(source.stem + "_slots.png")
        if dest.suffix.lower() != ".png":
            dest = dest / "slots.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        save_slot_overlay(source, slots, dest)
        print(f"[combo] wrote {dest}")
        return 0

    payload = detect_casts(
        source,
        fps=float(args.fps),
        champion_id=str(args.champion),
        summoners=parse_summoners(args.summoners),
        gap=float(args.gap),
        include_recast=not bool(args.no_recast),
        shift_x=int(args.shift_x),
        shift_y=int(args.shift_y),
        start=float(args.start),
        duration=float(args.duration) or None,
        debug_dir=args.debug_dir,
    )

    json_path = args.json or source.with_name(source.stem + "_combo.json")
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[combo] {len(payload['events'])} casts, {len(payload['combos'])} combos → {json_path}")
    for combo in payload["combos"]:
        chain = " > ".join(combo["chain"])
        print(f"        {combo['t0']:.2f}-{combo['t1']:.2f}s  ({combo['n']})  {chain}")

    want_overlay = bool(args.overlay or args.output)
    if want_overlay:
        out = args.output or source.with_name(source.stem + "_combo.mp4")
        render_overlay(source, payload, out, hold=float(args.hold))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
