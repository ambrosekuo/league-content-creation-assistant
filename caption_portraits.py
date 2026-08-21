#!/usr/bin/env python3
"""Caption dry 9:16 portraits, then optionally mix a music bed.

Portrait jobs now render without music. This step transcribes that dry file,
burns captions, then mixes a pool track onto a sibling so the captioned
video stays available for a different bed.

  python caption_portraits.py --dataset-dir data/aug17_2026_2849217240 --only gam14
  python caption_portraits.py --input data/.../gam14_..._portrait.mp4 --music auto
  python caption_portraits.py --input clip.mp4 --asr openai --music off
  python caption_portraits.py --input clip.mp4 --asr whisper --model small.en --music off --to es

Writes:
  {stem}_captioned.mp4          captions, no music
  {stem}_captioned_pool.mp4     captions + pool bed (unless --music off)
  {stem}_captions.json
  {stem}_captions.ass           (kept when --keep-ass)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from asr import (
    DEFAULT_WHISPER_MODEL,
    build_prompt,
    default_model_for,
    matchup_from_name,
    resolve_engine,
    transcribe_words,
)
from dataset_paths import find_dataset_dir, game_only_matches
from env_loader import load_dotenv
from ffmpeg_color import VIDEO_TO_BT709, X264_BT709

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

PORTRAIT_DIRS = (
    "lol_compilations_picks_portrait",
    "lol_compilations_portrait",
)
VARIANT_PRIORITY = ("_portrait", "_portrait_nomusic", "_portrait_pool", "_portrait_music")
ASR_VARIANTS = ("_portrait_nomusic", "_portrait")
SKIP_NAME_BITS = ("lobby", "preview", "_captioned", "nomusic")
MUSIC_STEM_SUFFIXES = ("_portrait_pool", "_portrait_music")

FONT_CANDIDATES = (
    ("/System/Library/Fonts/Supplemental/Arial Black.ttf", "Arial Black"),
    ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", "Arial"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", "Liberation Sans"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVu Sans"),
)

HIGHLIGHT_BGR = "0000FFFF"  # yellow
BODY_BGR = "00FFFFFF"  # white
OUTLINE_BGR = "00000000"

DEFAULT_MAX_WORDS = 3
DEFAULT_MAX_GAP = 0.42
DEFAULT_MAX_DUR = 2.3
DEFAULT_SKIP_TAIL = 2.5


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{detail}")


def _first_font() -> tuple[Path, str]:
    for path, name in FONT_CANDIDATES:
        font = Path(path)
        if font.is_file():
            return font, name
    raise FileNotFoundError("No caption font found (Arial Black / Arial / Liberation / DejaVu)")


def probe_video(path: Path) -> dict[str, Any]:
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
    payload = json.loads(proc.stdout or "{}")
    stream = (payload.get("streams") or [{}])[0]
    duration = float((payload.get("format") or {}).get("duration") or 0.0)
    return {
        "width": int(stream.get("width") or 1080),
        "height": int(stream.get("height") or 1920),
        "duration": duration,
    }


def ffmpeg_filter_path(path: Path) -> str:
    text = path.resolve().as_posix()
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", r"\'")


def extract_voice_wav(
    source: Path,
    dest: Path,
    *,
    audio_stream: int | None = None,
) -> None:
    """16 kHz mono with a light voice band so music beds hurt Whisper less."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-vn",
    ]
    if audio_stream is not None:
        cmd.extend(["-map", f"0:a:{audio_stream}"])
    cmd.extend(
        [
            "-ac",
            "1",
            "-ar",
            "16000",
            "-af",
            "highpass=f=160,lowpass=f=3800,dynaudnorm=f=150:g=11",
            "-c:a",
            "pcm_s16le",
            str(dest),
        ]
    )
    run(cmd)


def asr_source_for(target: Path, override: Path | None = None) -> Path:
    """Prefer a no-music sibling so the bed is not transcribed as lyrics."""
    if override is not None:
        path = override.resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        return path
    base = portrait_base(target.stem)
    folders = [target.parent]
    if target.parent.parent != target.parent:
        folders.append(target.parent.parent)
    game = re.match(r"(gam\d+)", target.stem, re.I)
    prefix = game.group(1) if game else None
    for folder in folders:
        for suffix in ASR_VARIANTS:
            sibling = folder / f"{base}{suffix}{target.suffix}"
            if sibling.is_file():
                return sibling
        if not prefix:
            continue
        for suffix in ASR_VARIANTS:
            hits = sorted(
                p
                for p in folder.glob(f"{prefix}_*{suffix}{target.suffix}")
                if p.stem.endswith(suffix)
            )
            if hits:
                return hits[0]
    return target


def group_words(
    words: list[dict[str, Any]],
    *,
    max_words: int,
    max_gap: float,
    max_dur: float,
    skip_head: float,
    skip_tail: float,
    duration: float,
) -> list[dict[str, Any]]:
    usable: list[dict[str, Any]] = []
    tail_cut = duration - max(0.0, skip_tail) if duration > 0 else 10**9
    for word in words:
        if word["end"] <= skip_head:
            continue
        if word["start"] >= tail_cut:
            continue
        clipped = dict(word)
        clipped["start"] = max(float(word["start"]), skip_head)
        clipped["end"] = min(float(word["end"]), tail_cut)
        if clipped["end"] - clipped["start"] < 0.05:
            continue
        usable.append(clipped)

    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for word in usable:
        if not current:
            current = [word]
            continue
        gap = float(word["start"]) - float(current[-1]["end"])
        dur = float(word["end"]) - float(current[0]["start"])
        if gap > max_gap or len(current) >= max_words or dur > max_dur:
            groups.append(current)
            current = [word]
        else:
            current.append(word)
    if current:
        groups.append(current)

    cues: list[dict[str, Any]] = []
    for idx, group in enumerate(groups):
        cues.append(
            {
                "id": idx,
                "start": round(float(group[0]["start"]), 3),
                "end": round(float(group[-1]["end"]), 3),
                "words": group,
                "text": " ".join(str(w["text"]) for w in group).strip(),
            }
        )
    return cues


def apply_case(text: str, mode: str) -> str:
    raw = " ".join(text.split())
    if mode == "upper":
        return raw.upper()
    if mode == "title":
        return raw.title()
    return raw


def translate_cues(cues: list[dict[str, Any]], *, target: str, source: str) -> list[dict[str, Any]]:
    load_dotenv()
    api_key = (
        os.environ.get("OPENAI_API_KEY")
        or os.environ.get("OPEN_API_KEY")
        or ""
    ).strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set (needed for --to translation)")
    model = os.environ.get("CAPTION_TRANSLATE_MODEL") or "gpt-4o-mini"
    lines = [{"id": c["id"], "text": c["text"]} for c in cues if c.get("text")]
    if not lines:
        return cues
    body = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "Translate short video captions. Keep meaning, keep them punchy, "
                    "do not add quotes or speaker labels. Return JSON "
                    '{"cues":[{"id":0,"text":"..."}]} with one item per input id.'
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"source": source, "target": target, "cues": lines},
                    ensure_ascii=False,
                ),
            },
        ],
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API error {exc.code}: {detail}") from exc

    content = (((payload.get("choices") or [{}])[0].get("message") or {}).get("content")) or "{}"
    parsed = json.loads(content)
    translated = {
        int(item["id"]): str(item.get("text") or "").strip()
        for item in (parsed.get("cues") or [])
        if isinstance(item, dict) and "id" in item
    }
    out: list[dict[str, Any]] = []
    for cue in cues:
        text = translated.get(int(cue["id"]), str(cue.get("text") or ""))
        out.append(spread_translated_cue(cue, text))
    return out


def spread_translated_cue(cue: dict[str, Any], text: str) -> dict[str, Any]:
    tokens = [tok for tok in re.split(r"\s+", text.strip()) if tok]
    start = float(cue["start"])
    end = float(cue["end"])
    if end <= start:
        end = start + 0.4
    if not tokens:
        return {**cue, "text": "", "words": []}
    weights = [max(1, len(tok)) for tok in tokens]
    total = float(sum(weights))
    span = end - start
    words: list[dict[str, Any]] = []
    cursor = start
    for i, tok in enumerate(tokens):
        piece = span * (weights[i] / total)
        word_end = end if i == len(tokens) - 1 else cursor + max(0.08, piece)
        words.append({"start": round(cursor, 3), "end": round(word_end, 3), "text": tok})
        cursor = word_end
    return {**cue, "text": " ".join(tokens), "words": words}


def ass_time(seconds: float) -> str:
    t = max(0.0, float(seconds))
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def ass_escape(text: str) -> str:
    return (
        str(text)
        .replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\n", r"\N")
    )


def styled_group(words: list[dict[str, Any]], active: int, *, case_mode: str) -> str:
    parts: list[str] = []
    for i, word in enumerate(words):
        token = ass_escape(apply_case(str(word["text"]), case_mode))
        if i == active:
            parts.append(rf"{{\c&H{HIGHLIGHT_BGR}&}}{token}{{\c&H{BODY_BGR}&}}")
        else:
            parts.append(token)
    return " ".join(parts)


def write_ass(
    cues: list[dict[str, Any]],
    dest: Path,
    *,
    width: int,
    height: int,
    font_name: str,
    case_mode: str,
) -> None:
    font_size = max(48, int(height * 0.044))
    outline = 5 if height >= 1600 else 4
    margin_v = max(160, int(height * 0.13))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{font_size},&H{BODY_BGR},&H000000FF,&H{OUTLINE_BGR},&H64000000,-1,0,0,0,100,100,0,0,1,{outline},0,2,48,48,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for cue in cues:
        words = list(cue.get("words") or [])
        if not words:
            continue
        for i, word in enumerate(words):
            text = styled_group(words, i, case_mode=case_mode)
            start = float(word["start"])
            end = float(word["end"])
            if i + 1 < len(words):
                end = max(end, float(words[i + 1]["start"]))
            if end <= start:
                continue
            lines.append(
                f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{text}\n"
            )
    dest.write_text("".join(lines), encoding="utf-8")


def burn_captions(
    source: Path,
    ass_path: Path,
    dest: Path,
    *,
    fonts_dir: Path,
    preset: str,
    crf: int,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    ass = ffmpeg_filter_path(ass_path)
    fonts = ffmpeg_filter_path(fonts_dir)
    vf = f"subtitles='{ass}':fontsdir='{fonts}',{VIDEO_TO_BT709}"
    tmp = dest.with_name(f"{dest.stem}._caption.mp4")
    try:
        run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-vf",
                vf,
                "-c:v",
                "libx264",
                "-preset",
                preset,
                "-crf",
                str(crf),
                *X264_BT709,
                "-c:a",
                "copy",
                "-movflags",
                "+faststart",
                str(tmp),
            ]
        )
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


def mix_music_bed(video: Path, music: str, *, music_db: float) -> dict[str, Any] | None:
    """Mix a pool/lofi bed onto a *copy* of the captioned file."""
    mode = str(music or "off").strip().lower()
    if mode in {"", "off", "none", "false", "0"}:
        return None
    if mode.startswith("lofi") or mode.startswith("bed"):
        from fetch_music import mix_bed, resolve_or_pick

        bed_query = "auto"
        if ":" in mode:
            bed_query = mode.split(":", 1)[1].strip() or "auto"
        chosen = resolve_or_pick(bed_query)
        dest = video.with_name(f"{video.stem}_lofi.mp4")
        mix_bed(video, chosen, dest, music_db=float(music_db))
        return {"id": chosen.get("id"), "output": str(dest), "db": music_db, "source": "lofi"}

    from music_pool import enabled_tracks, mix_pool_bed, resolve_pool_track

    pool_category = "multikill"
    pool_query = mode
    if mode.startswith("pool"):
        rest = mode.split(":", 1)[1].strip() if ":" in mode else "auto"
        known_ids = {str(t.get("id")).lower() for t in enabled_tracks()}
        if rest and rest not in {"", "auto", "random"} and rest not in known_ids:
            pool_category = rest
            pool_query = "auto"
        else:
            pool_query = rest or "auto"
    elif mode in {"auto", "random"}:
        pool_query = "auto"
    chosen = resolve_pool_track(pool_query, category=pool_category)
    dest = video.with_name(f"{video.stem}_pool.mp4")
    mix_pool_bed(video, chosen, dest, music_db=float(music_db))
    return {
        "id": chosen.get("id"),
        "name": chosen.get("name"),
        "output": str(dest),
        "db": music_db,
        "source": "pool",
    }


def portrait_base(stem: str) -> str:
    for suffix in ("_portrait_nomusic", "_portrait_pool", "_portrait_music", "_portrait"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    if stem.endswith("_captioned"):
        return stem[: -len("_captioned")]
    return stem


def is_portrait_mp4(path: Path, *, include_music: bool = False) -> bool:
    name = path.name.lower()
    if path.suffix.lower() != ".mp4" or not path.is_file():
        return False
    if any(bit in name for bit in SKIP_NAME_BITS):
        return False
    if not include_music and any(path.stem.endswith(sfx) for sfx in MUSIC_STEM_SUFFIXES):
        return False
    return "_portrait" in name


def discover_portraits(
    dataset_dir: Path,
    *,
    only: str,
    all_variants: bool,
) -> list[Path]:
    found: list[Path] = []
    for folder_name in PORTRAIT_DIRS:
        folder = dataset_dir / folder_name
        if not folder.is_dir():
            continue
        files = [p for p in sorted(folder.glob("*.mp4")) if is_portrait_mp4(p, include_music=True)]
        if only:
            files = [p for p in files if game_only_matches(p.name, only)]
        if all_variants:
            found.extend(files)
            continue
        by_base: dict[str, list[Path]] = {}
        for path in files:
            by_base.setdefault(portrait_base(path.stem), []).append(path)
        for group in by_base.values():
            picked = None
            for suffix in VARIANT_PRIORITY:
                picked = next((p for p in group if p.stem.endswith(suffix)), None)
                if picked is not None:
                    break
            found.append(picked or group[0])
    # De-dupe by resolved path, keep first dir (picks before auto weaves).
    seen: set[Path] = set()
    out: list[Path] = []
    for path in found:
        key = path.resolve()
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def caption_one(
    source: Path,
    *,
    output: Path | None,
    model_name: str,
    language: str,
    to_lang: str,
    device: str,
    compute_type: str,
    case_mode: str,
    max_words: int,
    max_gap: float,
    max_dur: float,
    skip_head: float,
    skip_tail: float,
    preset: str,
    crf: int,
    force: bool,
    force_transcribe: bool,
    ass_only: bool,
    keep_ass: bool,
    work_dir: Path | None,
    asr_input: Path | None = None,
    from_captions: Path | None = None,
    music: str = "auto",
    music_db: float = -18.0,
    asr_engine: str = "openai",
    asr_prompt: str = "",
    align: bool = True,
    align_model: str = DEFAULT_WHISPER_MODEL,
    audio_stream: int | None = None,
) -> dict[str, Any]:
    source = source.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    dest = (output or source.with_name(f"{source.stem}_captioned.mp4")).resolve()
    sidecar = dest.with_name(f"{source.stem}_captions.json")
    if dest.is_file() and not force and not ass_only:
        print(f"[skip] exists {dest.name} (pass --force)", flush=True)
        return {"source": str(source), "output": str(dest), "skipped": True}

    info = probe_video(source)
    font_path, font_name = _first_font()
    asr_path = asr_source_for(source, asr_input)
    detected = language if language != "auto" else "en"
    cues: list[dict[str, Any]] = []
    reuse = from_captions.resolve() if from_captions is not None else sidecar

    if reuse.is_file() and not force_transcribe:
        payload = json.loads(reuse.read_text(encoding="utf-8"))
        cues = list(payload.get("cues") or [])
        detected = str(payload.get("language") or detected)
        print(f"[caption] reuse {reuse.name} ({len(cues)} cues)", flush=True)
    else:
        engine = resolve_engine(asr_engine, default="openai")
        matchup = matchup_from_name(source.name)
        prompt = build_prompt(
            champion=matchup.get("champion") or "",
            opponent=matchup.get("opponent") or "",
            extra=asr_prompt,
        )
        print(
            f"[caption] asr {asr_path.name} engine={engine} model={model_name}",
            flush=True,
        )
        if matchup.get("champion") or matchup.get("opponent"):
            print(
                f"[caption] prompt champ={matchup.get('champion') or '-'} "
                f"vs={matchup.get('opponent') or '-'}",
                flush=True,
            )

        def _asr(tmp_root: Path) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
            wav_path = tmp_root / f"{source.stem}.wav"
            extract_voice_wav(asr_path, wav_path, audio_stream=audio_stream)
            lang = None if language == "auto" else language
            return transcribe_words(
                wav_path,
                engine=engine,
                model_name=model_name,
                language=lang,
                device=device,
                compute_type=compute_type,
                prompt=prompt,
                align=align,
                align_model=align_model,
            )

        asr_meta: dict[str, Any] = {}
        if work_dir is not None:
            work_dir.mkdir(parents=True, exist_ok=True)
            words, detected, asr_meta = _asr(work_dir)
        else:
            with tempfile.TemporaryDirectory(prefix="caption_") as raw:
                words, detected, asr_meta = _asr(Path(raw))
        cues = group_words(
            words,
            max_words=max_words,
            max_gap=max_gap,
            max_dur=max_dur,
            skip_head=skip_head,
            skip_tail=skip_tail,
            duration=float(info["duration"]),
        )
        overlay_lang = (to_lang or detected or "en").strip().lower()
        source_lang = (detected or "en").strip().lower()
        if overlay_lang and overlay_lang not in {source_lang, "source", "none"}:
            print(f"[caption] translate {source_lang} → {overlay_lang}", flush=True)
            cues = translate_cues(cues, target=overlay_lang, source=source_lang)
        else:
            overlay_lang = source_lang
        report = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": str(source),
            "asr_source": str(asr_path),
            "output": str(dest),
            "engine": asr_meta.get("engine") or asr_engine,
            "model": model_name,
            "prompt": asr_meta.get("prompt") or "",
            "raw_text": asr_meta.get("raw_text") or "",
            "align": asr_meta.get("align"),
            "align_model": asr_meta.get("align_model"),
            "language": overlay_lang,
            "source_language": source_lang,
            "cue_count": len(cues),
            "cues": cues,
        }
        sidecar.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"[caption] {len(cues)} cues → {sidecar.name}", flush=True)

    overlay_lang = (to_lang or detected or "en").strip().lower()
    ass_path = dest.with_name(f"{source.stem}_captions.ass")
    write_ass(
        cues,
        ass_path,
        width=int(info["width"]),
        height=int(info["height"]),
        font_name=font_name,
        case_mode=case_mode,
    )
    print(f"[caption] ass {ass_path.name} font={font_name}", flush=True)
    if ass_only:
        return {
            "source": str(source),
            "output": None,
            "ass": str(ass_path),
            "sidecar": str(sidecar),
            "cues": len(cues),
            "language": overlay_lang,
        }

    print(f"[caption] burn {source.name} → {dest.name}", flush=True)
    burn_captions(
        source,
        ass_path,
        dest,
        fonts_dir=font_path.parent,
        preset=preset,
        crf=crf,
    )
    if not keep_ass:
        ass_path.unlink(missing_ok=True)
    mix_query = music
    if str(music).strip().lower() in {"auto", "random"}:
        if any(source.stem.endswith(sfx) for sfx in MUSIC_STEM_SUFFIXES) or "pool_samples" in source.parts:
            print(
                "[caption] skip music mix (source already has a bed); pass --music TRACK to override",
                flush=True,
            )
            mix_query = "off"
    music_report = mix_music_bed(dest, mix_query, music_db=music_db)
    if music_report:
        print(
            f"[caption] music {music_report.get('id')} → {Path(str(music_report['output'])).name}",
            flush=True,
        )
    return {
        "source": str(source),
        "output": str(dest),
        "sidecar": str(sidecar),
        "cues": len(cues),
        "language": overlay_lang,
        "music": music_report,
        "skipped": False,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Transcribe a finished portrait and burn captions on top."
    )
    p.add_argument("--input", type=Path, help="Single portrait mp4, or a folder of mp4s")
    p.add_argument("--output", type=Path, help="Captioned mp4 (single --input only)")
    p.add_argument(
        "--asr-input",
        type=Path,
        help="Audio source for ASR (default: nomusic sibling, including parent dir)",
    )
    p.add_argument(
        "--asr",
        dest="asr_engine",
        default=None,
        choices=("whisper", "openai"),
        help="Caption ASR: openai (gpt-4o-mini-transcribe) or local whisper. "
        "Default: $CAPTION_ASR or openai",
    )
    p.add_argument(
        "--prompt",
        dest="asr_prompt",
        default="",
        help="Extra ASR prompt (League terms are added automatically from the filename)",
    )
    p.add_argument(
        "--align",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="When --asr openai, time words with local Whisper (default: on)",
    )
    p.add_argument(
        "--align-model",
        default=DEFAULT_WHISPER_MODEL,
        help="Local Whisper model used only to time OpenAI words",
    )
    p.add_argument(
        "--audio-stream",
        type=int,
        default=None,
        help="ffmpeg 0:a:N index if the file has a separate mic track",
    )
    p.add_argument(
        "--from-captions",
        type=Path,
        help="Reuse an existing *_captions.json instead of transcribing",
    )
    p.add_argument("--dataset-id", help="Dataset id under data/")
    p.add_argument("--dataset-dir", type=Path, help="Dataset folder")
    p.add_argument("--output-root", type=Path, default=DATA)
    p.add_argument("--only", default="", help="Only games whose name contains this (g14 / gam14)")
    p.add_argument(
        "--all-variants",
        action="store_true",
        help="Caption every *_portrait*.mp4 instead of preferring pool/music/plain",
    )
    p.add_argument(
        "--model",
        default=None,
        help="ASR model. Default: gpt-4o-mini-transcribe (openai) or small.en (whisper)",
    )
    p.add_argument("--language", default="en", help="ASR language, or auto")
    p.add_argument(
        "--to",
        default="",
        help="Overlay language (default: keep source). Example: es, pt, ja",
    )
    p.add_argument("--device", default="cpu", choices=["cpu", "cuda", "auto"])
    p.add_argument("--compute-type", default="int8")
    p.add_argument(
        "--case",
        dest="case_mode",
        default="upper",
        choices=("upper", "title", "keep"),
        help="Caption casing (default: upper, TikTok-style)",
    )
    p.add_argument("--max-words", type=int, default=DEFAULT_MAX_WORDS)
    p.add_argument("--max-gap", type=float, default=DEFAULT_MAX_GAP)
    p.add_argument("--max-dur", type=float, default=DEFAULT_MAX_DUR)
    p.add_argument("--skip-head", type=float, default=0.0, help="Do not caption the first N seconds")
    p.add_argument(
        "--skip-tail",
        type=float,
        default=DEFAULT_SKIP_TAIL,
        help="Do not caption the last N seconds (rank-card outro, default 2.5)",
    )
    p.add_argument("--preset", default="veryfast")
    p.add_argument("--crf", type=int, default=20)
    p.add_argument(
        "--music",
        default="auto",
        help="After captions: auto=pool pick (default), pool[:category], track id, lofi[:id], off",
    )
    p.add_argument("--music-db", type=float, default=-18.0)
    p.add_argument("--force", action="store_true", help="Overwrite captioned mp4")
    p.add_argument("--force-transcribe", action="store_true", help="Redo ASR even if sidecar exists")
    p.add_argument("--ass-only", action="store_true", help="Write JSON/ASS, skip the video burn")
    p.add_argument("--keep-ass", action="store_true")
    p.add_argument("--work-dir", type=Path, default=None)
    return p.parse_args()


def resolve_dataset(args: argparse.Namespace) -> Path | None:
    if args.dataset_dir is not None:
        return args.dataset_dir.resolve()
    if args.dataset_id:
        found = find_dataset_dir(args.output_root, args.dataset_id)
        if found:
            return found.resolve()
        return (args.output_root / args.dataset_id).resolve()
    return None


def main() -> int:
    load_dotenv()
    args = parse_args()
    try:
        if args.input is None and resolve_dataset(args) is None:
            print("Pass --input or --dataset-dir / --dataset-id.", file=sys.stderr)
            return 2
        if args.output is not None and (args.input is None or args.input.is_dir()):
            print("--output is only valid with a single file --input.", file=sys.stderr)
            return 2

        jobs: list[Path] = []
        if args.input is not None:
            inp = args.input.resolve()
            if inp.is_dir():
                jobs = sorted(
                    p
                    for p in inp.glob("*.mp4")
                    if p.is_file() and "_captioned" not in p.stem.lower()
                )
                if not jobs:
                    raise FileNotFoundError(f"No mp4s in {inp}")
            else:
                jobs = [inp]
        else:
            dataset_dir = resolve_dataset(args)
            assert dataset_dir is not None
            if not dataset_dir.is_dir():
                raise FileNotFoundError(dataset_dir)
            jobs = discover_portraits(
                dataset_dir,
                only=str(args.only or ""),
                all_variants=bool(args.all_variants),
            )
            if not jobs:
                raise FileNotFoundError(
                    f"No portrait mp4s under {dataset_dir} "
                    f"({', '.join(PORTRAIT_DIRS)})"
                )

        results: list[dict[str, Any]] = []
        shared_captions = args.from_captions
        single_file = bool(args.input and args.input.is_file())
        asr_engine = resolve_engine(
            args.asr_engine or os.environ.get("CAPTION_ASR"),
            default="openai",
        )
        model_name = str(args.model or default_model_for(asr_engine))
        for path in jobs:
            print(f"[caption] {path}", flush=True)
            result = caption_one(
                path,
                output=args.output if single_file else None,
                model_name=model_name,
                language=str(args.language),
                to_lang=str(args.to or "").strip().lower(),
                device=str(args.device),
                compute_type=str(args.compute_type),
                case_mode=str(args.case_mode),
                max_words=int(args.max_words),
                max_gap=float(args.max_gap),
                max_dur=float(args.max_dur),
                skip_head=float(args.skip_head),
                skip_tail=float(args.skip_tail),
                preset=str(args.preset),
                crf=int(args.crf),
                force=bool(args.force),
                force_transcribe=bool(args.force_transcribe) and shared_captions is None,
                ass_only=bool(args.ass_only),
                keep_ass=bool(args.keep_ass),
                work_dir=args.work_dir,
                asr_input=args.asr_input,
                from_captions=shared_captions,
                music=str(args.music),
                music_db=float(args.music_db),
                asr_engine=asr_engine,
                asr_prompt=str(args.asr_prompt or ""),
                align=bool(args.align),
                align_model=str(args.align_model or DEFAULT_WHISPER_MODEL),
                audio_stream=args.audio_stream,
            )
            results.append(result)
            if shared_captions is None and result.get("sidecar"):
                shared_captions = Path(str(result["sidecar"]))
        print(
            json.dumps(
                {
                    "status": "ok",
                    "count": len(results),
                    "results": results,
                },
                indent=2,
            )
        )
        return 0
    except Exception as exc:
        print(f"Caption failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
