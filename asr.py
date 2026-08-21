#!/usr/bin/env python3
"""Local Whisper and OpenAI transcription, with League vocabulary prompting.

gpt-4o-mini-transcribe does not return word timestamps, so caption timing is
taken from a cheap local Whisper pass and the OpenAI text is aligned onto it.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any

ENGINES = ("whisper", "openai")
DEFAULT_WHISPER_MODEL = "small.en"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini-transcribe"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"

CHAMPION_DISPLAY = {
    "aurelionsol": "Aurelion Sol",
    "belveth": "Bel'Veth",
    "chogath": "Cho'Gath",
    "drmundo": "Dr. Mundo",
    "jarvaniv": "Jarvan IV",
    "khazix": "Kha'Zix",
    "kogmaw": "Kog'Maw",
    "ksante": "K'Sante",
    "leblanc": "LeBlanc",
    "leesin": "Lee Sin",
    "masteryi": "Master Yi",
    "missfortune": "Miss Fortune",
    "monkeyking": "Wukong",
    "nunu": "Nunu",
    "reksai": "Rek'Sai",
    "tahmkench": "Tahm Kench",
    "twistedfate": "Twisted Fate",
    "velkoz": "Vel'Koz",
    "xinzhao": "Xin Zhao",
}

LOL_TERMS = (
    "LeBlanc",
    "Fizz",
    "Qiyana",
    "Yasuo",
    "Yone",
    "Akali",
    "Distortion",
    "Sigil",
    "Mimic",
    "Ethereal Chains",
    "Flash",
    "ignite",
    "teleport",
    "ult",
    "gank",
    "roam",
    "clone",
    "pad",
    "one-shot",
    "CS",
    "jungle",
    "mid",
    "Q",
    "W",
    "E",
    "R",
)

_VS_RE = re.compile(
    r"(?:^|_)(?:gam?\d+_)?([a-z][a-z0-9]+)_vs_([a-z][a-z0-9]+)",
    re.I,
)
_TOKEN_RE = re.compile(r"\S+")
_NORM_RE = re.compile(r"[^a-z0-9']+")


def resolve_engine(value: str | None, *, default: str) -> str:
    engine = (value or default or "whisper").strip().lower()
    if engine not in ENGINES:
        raise ValueError(f"Unknown ASR engine {engine!r}; use whisper or openai")
    return engine


def default_model_for(engine: str) -> str:
    if engine == "openai":
        return (
            os.environ.get("CAPTION_ASR_MODEL")
            or os.environ.get("OPENAI_TRANSCRIBE_MODEL")
            or DEFAULT_OPENAI_MODEL
        )
    return os.environ.get("WHISPER_MODEL") or DEFAULT_WHISPER_MODEL


def openai_api_key() -> str:
    return (
        os.environ.get("OPENAI_API_KEY")
        or os.environ.get("OPEN_API_KEY")
        or ""
    ).strip()


def champion_display(raw: str) -> str:
    key = re.sub(r"[^a-z0-9]", "", (raw or "").lower())
    if not key:
        return ""
    return CHAMPION_DISPLAY.get(key, key[:1].upper() + key[1:])


def matchup_from_name(name: str) -> dict[str, str]:
    stem = Path(name).stem
    hit = _VS_RE.search(stem.replace("-", "_"))
    if not hit:
        return {"champion": "", "opponent": ""}
    return {
        "champion": champion_display(hit.group(1)),
        "opponent": champion_display(hit.group(2)),
    }


def build_prompt(
    *,
    champion: str = "",
    opponent: str = "",
    extra: str = "",
) -> str:
    parts = ["League of Legends streamer talking about the play."]
    if champion and opponent:
        parts.append(f"Champion: {champion}. Opponent: {opponent}.")
    elif champion:
        parts.append(f"Champion: {champion}.")
    vocab = list(LOL_TERMS)
    if champion and champion not in vocab:
        vocab.insert(0, champion)
    if opponent and opponent not in vocab:
        vocab.insert(1, opponent)
    parts.append("Vocabulary: " + ", ".join(vocab) + ".")
    extra = extra.strip()
    if extra:
        parts.append(extra)
    return " ".join(parts)


def audio_duration(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
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
    return float((payload.get("format") or {}).get("duration") or 0.0)


def tokenize_text(text: str) -> list[str]:
    return [tok for tok in _TOKEN_RE.findall(text.strip()) if tok]


def _norm(token: str) -> str:
    return _NORM_RE.sub("", token.lower())


def words_from_text(
    text: str,
    *,
    start: float,
    end: float,
    compress: bool = False,
) -> list[dict[str, Any]]:
    tokens = tokenize_text(text)
    if not tokens:
        return []
    if end <= start:
        end = start + max(0.18 * len(tokens), 0.4)
    n = len(tokens)
    if compress:
        max_span = min(0.28 * n, 2.2)
        if end - start > max_span:
            end = start + max_span
    weights = [max(1, len(tok)) for tok in tokens]
    total = float(sum(weights))
    span = max(end - start, 0.08 * n)
    words: list[dict[str, Any]] = []
    cursor = start
    for i, tok in enumerate(tokens):
        piece = span * (weights[i] / total)
        word_end = start + span if i == len(tokens) - 1 else cursor + max(0.08, piece)
        words.append(
            {
                "start": round(cursor, 3),
                "end": round(max(word_end, cursor + 0.08), 3),
                "text": tok,
            }
        )
        cursor = words[-1]["end"]
    return words


def _dedupe_timed(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for word in words:
        start = float(word["start"])
        end = float(word["end"])
        text = str(word.get("text") or "")
        if out:
            prev = out[-1]
            same_slot = abs(start - float(prev["start"])) < 0.04 and abs(
                end - float(prev["end"])
            ) < 0.04
            if same_slot and _norm(text) == _norm(str(prev.get("text") or "")):
                continue
            if same_slot:
                continue
        out.append({"start": start, "end": end, "text": text})
    return out


def _repair_monotonic(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor = 0.0
    for word in words:
        start = max(float(word["start"]), cursor)
        end = max(float(word["end"]), start + 0.08)
        out.append(
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "text": word["text"],
            }
        )
        cursor = end
    return out


def words_to_segments(
    words: list[dict[str, Any]],
    *,
    gap: float = 0.75,
) -> list[dict[str, Any]]:
    if not words:
        return []
    groups: list[list[dict[str, Any]]] = [[words[0]]]
    for word in words[1:]:
        if float(word["start"]) - float(groups[-1][-1]["end"]) > gap:
            groups.append([word])
        else:
            groups[-1].append(word)
    segments: list[dict[str, Any]] = []
    for group in groups:
        text = " ".join(str(w["text"]) for w in group).strip()
        if not text:
            continue
        segments.append(
            {
                "start": round(float(group[0]["start"]), 3),
                "end": round(float(group[-1]["end"]), 3),
                "text": text,
            }
        )
    return segments


def align_text_to_words(
    text: str,
    timed: list[dict[str, Any]],
    *,
    duration: float | None = None,
) -> list[dict[str, Any]]:
    target = tokenize_text(text)
    if not target:
        return []
    timed = _dedupe_timed(timed)
    if not timed:
        return words_from_text(text, start=0.0, end=float(duration or 0.0) or 1.0)

    src_norm = [_norm(str(w.get("text") or "")) for w in timed]
    tgt_norm = [_norm(tok) for tok in target]
    matcher = SequenceMatcher(a=src_norm, b=tgt_norm, autojunk=False)
    out: list[dict[str, Any]] = []
    silence_gap = 0.85

    def _span(i1: int, i2: int) -> tuple[float, float, bool]:
        """Return start, end, and whether this is a real Whisper range (not a silence hole)."""
        if i1 < i2 and i2 <= len(timed):
            return float(timed[i1]["start"]), float(timed[i2 - 1]["end"]), True
        prev_end = float(timed[i1 - 1]["end"]) if i1 > 0 else 0.0
        next_start = float(timed[i1]["start"]) if i1 < len(timed) else prev_end + 0.3
        gap = next_start - prev_end
        if gap > silence_gap:
            return prev_end, prev_end, False
        if gap <= 0:
            return prev_end, prev_end + 0.24, False
        return prev_end, next_start, True

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for src_i, tgt_i in zip(range(i1, i2), range(j1, j2)):
                word = dict(timed[src_i])
                word["text"] = target[tgt_i]
                out.append(word)
            continue
        if tag == "delete":
            continue
        start, end, spoken = _span(i1, i2)
        out.extend(
            words_from_text(
                " ".join(target[j1:j2]),
                start=start,
                end=end,
                compress=not spoken,
            )
        )
    return _repair_monotonic(out)


@lru_cache(maxsize=4)
def _whisper_model(model_name: str, device: str, compute_type: str):
    from faster_whisper import WhisperModel

    return WhisperModel(model_name, device=device, compute_type=compute_type)


def whisper_words(
    audio_path: Path,
    *,
    model_name: str,
    language: str | None,
    device: str,
    compute_type: str,
    prompt: str,
    word_timestamps: bool = True,
    filter_low_confidence: bool = True,
) -> tuple[list[dict[str, Any]], str]:
    model = _whisper_model(model_name, device, compute_type)
    segments_iter, info = model.transcribe(
        str(audio_path),
        language=language,
        vad_filter=True,
        word_timestamps=word_timestamps,
        condition_on_previous_text=False,
        initial_prompt=prompt or None,
    )
    detected = str(getattr(info, "language", None) or language or "en")
    words: list[dict[str, Any]] = []
    for segment in segments_iter:
        if filter_low_confidence:
            no_speech = float(getattr(segment, "no_speech_prob", 0.0) or 0.0)
            avg_logprob = float(getattr(segment, "avg_logprob", 0.0) or 0.0)
            if no_speech > 0.65 or avg_logprob < -1.15:
                continue
        timed_words = list(getattr(segment, "words", None) or [])
        if timed_words:
            for word in timed_words:
                text = str(getattr(word, "word", "") or "").strip()
                if not text:
                    continue
                start = float(word.start)
                end = float(word.end)
                if end <= start:
                    end = start + 0.12
                words.append(
                    {
                        "start": round(start, 3),
                        "end": round(end, 3),
                        "text": text,
                    }
                )
            continue
        text = str(getattr(segment, "text", "") or "").strip()
        if not text:
            continue
        words.extend(
            words_from_text(
                text,
                start=float(segment.start),
                end=float(segment.end),
            )
        )
    return words, detected


def _post_multipart(
    url: str,
    *,
    api_key: str,
    fields: dict[str, str],
    file_path: Path,
    timeout: float,
) -> dict[str, Any]:
    boundary = "----AsrBoundary" + secrets.token_hex(16)
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8")
        )
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")
    filename = file_path.name
    suffix = file_path.suffix.lower()
    mime = "audio/wav" if suffix == ".wav" else "audio/mpeg" if suffix == ".mp3" else "application/octet-stream"
    chunks.append(f"--{boundary}\r\n".encode("utf-8"))
    chunks.append(
        (
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
    )
    chunks.append(file_path.read_bytes())
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(chunks)
    last_exc: Exception | None = None
    for attempt in range(1, 5):
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI transcription error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            last_exc = exc
            if attempt == 4:
                break
            wait = 2 ** attempt
            print(
                f"[asr] openai upload failed ({exc}); retry {attempt}/3 in {wait}s",
                flush=True,
            )
            time.sleep(wait)
    raise RuntimeError(f"OpenAI transcription request failed: {last_exc}") from last_exc


def openai_transcribe_text(
    audio_path: Path,
    *,
    model_name: str,
    language: str | None,
    prompt: str,
    timeout: float = 180.0,
) -> tuple[str, str]:
    api_key = openai_api_key()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set (needed for --asr openai)")
    base = (
        os.environ.get("OPENAI_BASE_URL") or DEFAULT_OPENAI_BASE_URL
    ).rstrip("/")
    fields = {
        "model": model_name,
        "response_format": "json",
    }
    if language:
        fields["language"] = language
    if prompt:
        fields["prompt"] = prompt
    payload = _post_multipart(
        f"{base}/audio/transcriptions",
        api_key=api_key,
        fields=fields,
        file_path=audio_path,
        timeout=timeout,
    )
    text = str(payload.get("text") or "").strip()
    lang = language or "en"
    languages = payload.get("languages")
    if isinstance(languages, list) and languages:
        first = languages[0]
        if isinstance(first, dict) and first.get("code"):
            lang = str(first["code"])
        elif isinstance(first, str):
            lang = first
    elif payload.get("language"):
        lang = str(payload["language"])
    return text, lang


def transcribe_words(
    audio_path: Path,
    *,
    engine: str,
    model_name: str,
    language: str | None,
    device: str = "cpu",
    compute_type: str = "int8",
    prompt: str = "",
    align: bool = True,
    align_model: str = DEFAULT_WHISPER_MODEL,
    filter_low_confidence: bool = True,
) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    engine = resolve_engine(engine, default="whisper")
    meta: dict[str, Any] = {
        "engine": engine,
        "model": model_name,
        "prompt": prompt,
        "align": bool(align and engine == "openai"),
        "raw_text": "",
    }
    if engine == "whisper":
        words, detected = whisper_words(
            audio_path,
            model_name=model_name,
            language=language,
            device=device,
            compute_type=compute_type,
            prompt=prompt,
            filter_low_confidence=filter_low_confidence,
        )
        meta["raw_text"] = " ".join(str(w["text"]) for w in words).strip()
        return words, detected, meta

    text, detected = openai_transcribe_text(
        audio_path,
        model_name=model_name,
        language=language,
        prompt=prompt,
    )
    meta["raw_text"] = text
    if not text:
        return [], detected, meta
    timed: list[dict[str, Any]] = []
    if align:
        meta["align_model"] = align_model
        try:
            timed, _ignored = whisper_words(
                audio_path,
                model_name=align_model,
                language=language or detected,
                device=device,
                compute_type=compute_type,
                # Do not feed the OpenAI transcript back as Whisper's initial_prompt.
                # That collapses early word timestamps (captions start late and then race).
                prompt=prompt,
                filter_low_confidence=False,
            )
        except ImportError as exc:
            print(f"[asr] skip whisper align ({exc}); using even word timings", flush=True)
            meta["align"] = False
    duration = audio_duration(audio_path)
    words = align_text_to_words(text, timed, duration=duration)
    return words, detected, meta
