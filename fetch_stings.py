#!/usr/bin/env python3
"""Local Freesound sting pack: inbox, suggested, and intro/.

  python fetch_stings.py              # status
  python fetch_stings.py --list       # preview CC0 hits (needs FREESOUND_API_KEY)
  python fetch_stings.py --suggest    # dopamine picks → suggested/
  python fetch_stings.py --suggest --slot intro   # intro idents → intro/
  python fetch_stings.py --adopt FILE # copy a download into inbox/
  python fetch_stings.py --adopt FILE --slot intro
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from env_loader import load_dotenv

ROOT = Path(__file__).resolve().parent
PACK_DIR = ROOT / "assets" / "stings"
SLOTS_PATH = PACK_DIR / "slots.json"
MANIFEST_PATH = PACK_DIR / "manifest.json"
INBOX_DIR = PACK_DIR / "inbox"
SUGGEST_DIR = PACK_DIR / "suggested"
INTRO_DIR = PACK_DIR / "intro"
API = "https://freesound.org/apiv2"
FIELDS = (
    "id,name,username,license,duration,num_downloads,avg_rating,"
    "previews,url,tags,type"
)
USER_AGENT = "lolambrosek-stings/1.0"
AUDIO_SUFFIX = {".wav", ".mp3", ".ogg", ".flac", ".aiff", ".aif", ".m4a"}
ID_PREFIX_RE = re.compile(r"^(\d+)__")


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Missing file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_cc0(license_text: str) -> bool:
    text = (license_text or "").lower()
    return (
        "creative commons 0" in text
        or "publicdomain/zero" in text
        or "cc0" in text.replace(" ", "")
    )


def http_json(url: str, *, token: str, timeout: float = 30.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Token {token}", "User-Agent": USER_AGENT},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {exc.code} for {url}: {detail or exc.reason}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from None


def http_download(url: str, dest: Path, *, token: str | None = None) -> None:
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Token {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(req, timeout=60.0) as resp, dest.open("wb") as out:
        shutil.copyfileobj(resp, out)


def duration_filter(lo: float, hi: float) -> str:
    return f'license:"Creative Commons 0" duration:[{lo} TO {hi}]'


def web_search_url(query: str, lo: float, hi: float) -> str:
    return "https://freesound.org/search/?" + urllib.parse.urlencode(
        {"q": query, "f": duration_filter(lo, hi), "s": "downloads_desc"}
    )


def require_token() -> str:
    token = (os.environ.get("FREESOUND_API_KEY") or "").strip()
    if not token:
        raise RuntimeError(
            "set FREESOUND_API_KEY in .env "
            "(https://freesound.org/apiv2/apply)"
        )
    return token


def search_query(
    token: str,
    query: str,
    *,
    lo: float,
    hi: float,
    page_size: int,
    sort: str,
) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {
            "query": query,
            "filter": duration_filter(lo, hi),
            "sort": sort,
            "fields": FIELDS,
            "page_size": str(page_size),
        }
    )
    payload = http_json(f"{API}/search/?{params}", token=token)
    return [
        r
        for r in (payload.get("results") or [])
        if is_cc0(str(r.get("license") or ""))
    ]


def search_slot(
    token: str,
    slot: dict[str, Any],
    *,
    lo: float,
    hi: float,
    page_size: int,
    sort: str,
) -> tuple[str, list[dict[str, Any]]]:
    last_query = ""
    seen: set[int] = set()
    merged: list[dict[str, Any]] = []
    for query in slot.get("queries") or []:
        last_query = str(query)
        for sound in search_query(
            token, last_query, lo=lo, hi=hi, page_size=page_size, sort=sort
        ):
            sid = int(sound.get("id") or 0)
            if not sid or sid in seen:
                continue
            seen.add(sid)
            merged.append(sound)
        time.sleep(0.35)
    return last_query, merged


def to_wav(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-ac",
        "2",
        "-ar",
        "48000",
        "-c:a",
        "pcm_s16le",
        str(dest),
    ]
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"ffmpeg failed: {detail}")


def preview_url(sound: dict[str, Any]) -> str:
    previews = sound.get("previews") or {}
    for key in ("preview-hq-mp3", "preview-hq-ogg", "preview-lq-mp3"):
        url = previews.get(key)
        if url:
            return str(url)
    raise RuntimeError(f"sound {sound.get('id')} has no preview URL")


def load_manifest() -> dict[str, Any]:
    if MANIFEST_PATH.is_file():
        return load_json(MANIFEST_PATH)
    return {
        "source": "freesound",
        "license_policy": "Creative Commons 0 only",
        "inbox": [],
        "suggested": [],
        "intro": [],
    }


def slot_dest(slot_id: str) -> tuple[Path, str]:
    if slot_id == "intro":
        return INTRO_DIR, "intro"
    return SUGGEST_DIR, "suggested"


def sting_filename(slot_id: str, sound: dict[str, Any]) -> str:
    sid = int(sound.get("id") or 0)
    name = slug(str(sound.get("name") or "sting"))
    if slot_id == "intro":
        return f"{sid}_{name}.wav"
    return f"{slot_id}_{sid}_{name}.wav"


def save_manifest(manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = now_iso()
    write_json(MANIFEST_PATH, manifest)


def audio_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(
        p
        for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_SUFFIX
    )


def parse_freesound_id(path: Path) -> int | None:
    m = ID_PREFIX_RE.match(path.name)
    return int(m.group(1)) if m else None


def slug(text: str, *, fallback: str = "sting") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return (cleaned[:40] or fallback)


def fmt_sound(rank: int, sound: dict[str, Any]) -> str:
    dur = float(sound.get("duration") or 0.0)
    dl = int(sound.get("num_downloads") or 0)
    name = sound.get("name") or "(untitled)"
    user = sound.get("username") or "?"
    url = sound.get("url") or f"https://freesound.org/s/{sound.get('id')}/"
    return (
        f"  {rank}.  {dur:5.2f}s  {dl:6d} dl  \"{name}\"  by {user}\n"
        f"      {url}"
    )


def fetch_sound(token: str, sound_id: int) -> dict[str, Any]:
    params = urllib.parse.urlencode({"fields": FIELDS})
    return http_json(f"{API}/sounds/{sound_id}/?{params}", token=token)


def entry_from_sound(
    sound: dict[str, Any],
    *,
    folder: str,
    file: str,
    slot: str | None = None,
    preview: str | None = None,
) -> dict[str, Any]:
    return {
        "slot": slot,
        "folder": folder,
        "freesound_id": sound.get("id"),
        "name": sound.get("name"),
        "username": sound.get("username"),
        "license": sound.get("license"),
        "url": sound.get("url") or f"https://freesound.org/s/{sound.get('id')}/",
        "duration": sound.get("duration"),
        "file": file,
        "preview": preview,
        "fetched_at": now_iso(),
    }


def upsert(manifest: dict[str, Any], key: str, entry: dict[str, Any]) -> None:
    rows = [r for r in (manifest.get(key) or []) if r.get("file") != entry["file"]]
    rows.append(entry)
    rows.sort(key=lambda r: str(r.get("file") or ""))
    manifest[key] = rows


def cmd_list(
    token: str | None,
    slots: list[dict[str, Any]],
    *,
    lo: float,
    hi: float,
    page_size: int,
    sort: str,
    only: str | None,
) -> int:
    if token is None:
        print("No FREESOUND_API_KEY — browse these CC0 searches:\n")
        for slot in slots:
            if only and slot["id"] != only:
                continue
            query = (slot.get("queries") or ["sound"])[0]
            print(f"[{slot['id']}] {slot['label']}  —  {slot['use_on']}")
            print(f"  {web_search_url(str(query), lo, hi)}\n")
        return 0

    for i, slot in enumerate(slots):
        if only and slot["id"] != only:
            continue
        if i:
            time.sleep(0.35)
        query, results = search_slot(
            token, slot, lo=lo, hi=hi, page_size=page_size, sort=sort
        )
        print(f"[{slot['id']}] {slot['label']}  query={query!r}  ({len(results)} shown)")
        print(f"  use on: {slot['use_on']}")
        if not results:
            print("  (no CC0 hits)")
            print(f"  {web_search_url(query or slot['id'], lo, hi)}\n")
            continue
        for rank, sound in enumerate(results[:page_size], start=1):
            print(fmt_sound(rank, sound))
        print()
    return 0


def download_preview_wav(sound: dict[str, Any], dest: Path, *, token: str) -> str:
    url = preview_url(sound)
    suffix = Path(urllib.parse.urlparse(url).path).suffix or ".mp3"
    with tempfile.TemporaryDirectory(prefix="sting-") as tmp:
        raw = Path(tmp) / f"preview{suffix}"
        http_download(url, raw, token=token)
        to_wav(raw, dest)
    return url


def cmd_suggest(
    token: str,
    slots: list[dict[str, Any]],
    *,
    lo: float,
    hi: float,
    page_size: int,
    sort: str,
    per_slot: int,
    only: str | None,
    force: bool,
) -> int:
    INTRO_DIR.mkdir(parents=True, exist_ok=True)
    SUGGEST_DIR.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()
    have_ids = {
        int(r["freesound_id"])
        for key in ("suggested", "intro")
        for r in (manifest.get(key) or [])
        if r.get("freesound_id")
    }
    saved = 0
    last_folder = SUGGEST_DIR
    for i, slot in enumerate(slots):
        if only and slot["id"] != only:
            continue
        dest_dir, folder = slot_dest(str(slot["id"]))
        last_folder = dest_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        if i:
            time.sleep(0.35)
        _query, results = search_slot(
            token, slot, lo=lo, hi=hi, page_size=page_size, sort=sort
        )
        picked = 0
        for sound in results:
            if picked >= per_slot:
                break
            sid = int(sound.get("id") or 0)
            dest = dest_dir / sting_filename(str(slot["id"]), sound)
            if dest.is_file() and not force:
                print(f"[{slot['id']}] have {dest.name}")
                picked += 1
                continue
            if sid in have_ids and dest.is_file() and not force:
                picked += 1
                continue
            preview = download_preview_wav(sound, dest, token=token)
            entry = entry_from_sound(
                sound,
                folder=folder,
                file=str(dest.relative_to(ROOT)),
                slot=str(slot["id"]),
                preview=preview,
            )
            upsert(manifest, folder, entry)
            have_ids.add(sid)
            print(f"[{slot['id']}] {dest.name}")
            print(f"         {sound.get('name')}  by {sound.get('username')}")
            print(f"         {entry['url']}")
            picked += 1
            saved += 1
            time.sleep(0.25)
        if picked == 0:
            print(f"[{slot['id']}] no CC0 hits")
    save_manifest(manifest)
    print(f"\nsaved {saved} file(s) → {last_folder.relative_to(ROOT)}/")
    return 0


def cmd_adopt(
    path: Path,
    *,
    token: str | None,
    sound_id: int | None,
    url: str | None,
    slot: str | None = None,
) -> int:
    if not path.is_file():
        raise RuntimeError(f"file not found: {path}")
    if path.suffix.lower() not in AUDIO_SUFFIX:
        raise RuntimeError(f"not an audio file: {path.name}")
    dest_dir, folder = (INTRO_DIR, "intro") if slot == "intro" else (INBOX_DIR, "inbox")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    if path.resolve() != dest.resolve():
        shutil.copy2(path, dest)
    sid = sound_id or parse_freesound_id(path)
    sound: dict[str, Any] = {
        "id": sid,
        "name": path.stem,
        "username": None,
        "license": None,
        "url": url or (f"https://freesound.org/s/{sid}/" if sid else None),
        "duration": None,
    }
    if sid and token:
        try:
            sound = fetch_sound(token, sid)
        except RuntimeError as exc:
            print(f"warning: could not look up {sid}: {exc}")
    if sound.get("license") and not is_cc0(str(sound["license"])):
        print(f"warning: {path.name} license is {sound['license']} (not CC0)")
    entry = entry_from_sound(
        sound,
        folder=folder,
        file=str(dest.relative_to(ROOT)),
        slot=slot,
    )
    manifest = load_manifest()
    upsert(manifest, folder, entry)
    save_manifest(manifest)
    print(f"{folder} ← {dest.name}")
    if entry.get("url"):
        print(f"         {entry['url']}")
    if entry.get("license"):
        print(f"         {entry['license']}")
    return 0


def cmd_status(slots: list[dict[str, Any]]) -> int:
    sections = (
        (INBOX_DIR, "drop your downloads here"),
        (SUGGEST_DIR, "API dopamine picks"),
        (INTRO_DIR, "intro / ident stings"),
    )
    for folder, note in sections:
        print(f"{folder.relative_to(ROOT)}/  ← {note}")
        files = audio_files(folder)
        if files:
            for p in files:
                print(f"  {p.name}")
        else:
            print("  (empty)")
        print()
    print("search recipes:")
    for slot in slots:
        print(f"  {slot['id']:10}  {slot['label']:18}  {slot['use_on']}")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Inbox + suggested Freesound sting pack"
    )
    p.add_argument("--list", action="store_true", help="Show top CC0 matches")
    p.add_argument(
        "--suggest",
        action="store_true",
        help="Download dopamine-like CC0 picks into suggested/",
    )
    p.add_argument(
        "--fetch",
        action="store_true",
        help="Alias for --suggest",
    )
    p.add_argument("--status", action="store_true", help="Show inbox + suggested")
    p.add_argument(
        "--adopt",
        metavar="PATH",
        help="Copy a local download into inbox/",
    )
    p.add_argument("--slot", default=None, help="Only this suggestion recipe")
    p.add_argument("--id", type=int, default=None, dest="sound_id")
    p.add_argument("--url", default=None)
    p.add_argument("--force", action="store_true")
    p.add_argument("--page-size", type=int, default=8)
    p.add_argument("--per-slot", type=int, default=None, help="Picks per recipe")
    return p.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()
    cfg = load_json(SLOTS_PATH)
    slots = list(cfg.get("slots") or [])
    if not slots:
        print("error: assets/stings/slots.json has no slots", file=sys.stderr)
        return 1
    lo, hi = (cfg.get("duration") or [0.15, 2.5])[:2]
    lo, hi = float(lo), float(hi)
    sort = str(cfg.get("sort") or "downloads_desc")
    per_slot = int(args.per_slot or cfg.get("per_slot") or 2)
    known = {str(s["id"]) for s in slots}
    if args.slot and args.slot not in known:
        print(
            f"error: unknown slot {args.slot!r} — {', '.join(sorted(known))}",
            file=sys.stderr,
        )
        return 1

    token = (os.environ.get("FREESOUND_API_KEY") or "").strip() or None
    try:
        if args.adopt:
            return cmd_adopt(
                Path(args.adopt).expanduser(),
                token=token,
                sound_id=args.sound_id,
                url=args.url,
                slot=args.slot,
            )
        if args.list:
            return cmd_list(
                token,
                slots,
                lo=lo,
                hi=hi,
                page_size=args.page_size,
                sort=sort,
                only=args.slot,
            )
        if args.suggest or args.fetch:
            return cmd_suggest(
                require_token(),
                slots,
                lo=lo,
                hi=hi,
                page_size=args.page_size,
                sort=sort,
                per_slot=per_slot,
                only=args.slot,
                force=args.force,
            )
        return cmd_status(slots)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
