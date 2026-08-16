#!/usr/bin/env python3
"""Local Wikimedia Commons GIF pack: inbox (your drops) + suggested (API picks).

Openverse has almost no climbing GIFs, so this searches Commons for animated GIFs
and short CC videos, then ffmpeg-clips videos into looping GIFs.

  python fetch_gifs.py              # status
  python fetch_gifs.py --list       # preview GIF / video hits
  python fetch_gifs.py --suggest    # download / convert → suggested/
  python fetch_gifs.py --adopt FILE # copy a GIF into inbox/
"""

from __future__ import annotations

import argparse
import html
import json
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
PACK_DIR = ROOT / "assets" / "gifs"
SLOTS_PATH = PACK_DIR / "slots.json"
MANIFEST_PATH = PACK_DIR / "manifest.json"
INBOX_DIR = PACK_DIR / "inbox"
SUGGEST_DIR = PACK_DIR / "suggested"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "lolambrosek-gifs/1.0"
GIF_SUFFIX = {".gif"}
IMAGE_SUFFIX = {".gif", ".webp", ".apng"}
OK_MIME = {
    "image/gif",
    "video/webm",
    "video/mp4",
    "video/quicktime",
}
SKIP_TITLE = (
    "chasseurs",
    "metro alpin",
    "700km",
    "greenland",
    "everest is melting",
    "wingsuit",
    "14 juillet",
    "felin",
    "gorilla",
    "ocicat",
    "firefighter",
    "fern",
    "nightshade",
    "annealing",
    "roadster",
    "monkey",
    "gecko",
    "olympic",
    "obverse",
    "caterpillar",
)
RELEVANCE_STRONG = (
    "climb",
    "climber",
    "climbing",
    "alpin",
    "ice climbing",
    "rock climbing",
    "mountaineer",
    "mountaineering",
    "jumar",
    "glacier",
    "steck",
    "eiger",
)
RELEVANCE_WEAK = ("mountain", "peak", "summit", "ice", "route", "couloir", "drus")
HTML_TAG_RE = re.compile(r"<[^>]+>")


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Missing file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def strip_html(text: str) -> str:
    cleaned = HTML_TAG_RE.sub(" ", html.unescape(text or ""))
    return re.sub(r"\s+", " ", cleaned).strip()


def clean_title(text: str) -> str:
    title = strip_html(text)
    if title.lower().startswith("file:"):
        title = title[5:]
    return urllib.parse.unquote(title)


def slug(text: str, *, fallback: str = "gif") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", clean_title(text)).strip("_").lower()
    return (cleaned[:40] or fallback)


def meta_value(meta: dict[str, Any] | None, key: str) -> str:
    raw = (meta or {}).get(key) or {}
    if isinstance(raw, dict):
        return strip_html(str(raw.get("value") or ""))
    return strip_html(str(raw or ""))


def is_ok_license(license_text: str) -> bool:
    text = (license_text or "").lower()
    compact = text.replace(" ", "")
    if "nc" in compact or "nd" in compact:
        return False
    return (
        "cc0" in compact
        or "publicdomain" in compact
        or "public domain" in text
        or "ccby" in compact
        or "attribution" in text
        or "sharealike" in compact
        or "share alike" in text
        or compact in {"pd", "pdm"}
    )


def http_json(url: str, *, timeout: float = 30.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        method="GET",
    )
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code != 429 or attempt == 4:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                raise RuntimeError(
                    f"HTTP {exc.code} for {url}: {detail or exc.reason}"
                ) from None
            time.sleep(5.0 * (2**attempt))
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Network error: {exc.reason}") from None
    raise RuntimeError(f"Network error: {last_error}")


def clean_media_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse(parsed._replace(query="", fragment=""))


def transcode_url(url: str, height: str = "360p") -> str | None:
    parsed = urllib.parse.urlparse(clean_media_url(url))
    parts = parsed.path.split("/")
    if len(parts) < 6 or "commons" not in parts:
        return None
    filename = parts[-1]
    a, ab = parts[-3], parts[-2]
    return (
        f"https://upload.wikimedia.org/wikipedia/commons/transcoded/"
        f"{a}/{ab}/{filename}/{filename}.{height}.vp9.webm"
    )


def clip_source_url(hit: dict[str, Any]) -> str:
    raw = clean_media_url(str(hit.get("url") or ""))
    if str(hit.get("mime") or "") == "image/gif":
        return raw
    return transcode_url(raw, "360p") or transcode_url(raw, "240p") or raw


def http_download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}, method="GET"
    )
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=90.0) as resp, dest.open("wb") as out:
                shutil.copyfileobj(resp, out)
            if dest.is_file() and dest.stat().st_size > 100:
                return
            raise RuntimeError(f"empty download: {url}")
        except urllib.error.HTTPError as exc:
            last_error = exc
            dest.unlink(missing_ok=True)
            if exc.code != 429 or attempt == 3:
                raise RuntimeError(f"HTTP {exc.code} downloading {url}: {exc.reason}") from None
            time.sleep(8.0 * (attempt + 1))
        except urllib.error.URLError as exc:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"Network error: {exc.reason}") from None
    raise RuntimeError(f"download failed: {last_error}")


def web_search_url(query: str) -> str:
    return "https://commons.wikimedia.org/w/index.php?" + urllib.parse.urlencode(
        {"search": query, "title": "Special:MediaSearch", "type": "video"}
    )


def commons_search(query: str, *, page_size: int) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": query,
            "gsrnamespace": "6",
            "gsrlimit": str(page_size),
            "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata|dimensions",
        }
    )
    payload = http_json(f"{COMMONS_API}?{params}")
    pages = (payload.get("query") or {}).get("pages") or {}
    hits: list[dict[str, Any]] = []
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        hits.append(
            {
                "id": str(page.get("pageid") or ""),
                "title": page.get("title") or "",
                "mime": info.get("mime") or "",
                "url": info.get("url") or "",
                "landing": info.get("descriptionurl")
                or f"https://commons.wikimedia.org/wiki/{urllib.parse.quote(page.get('title') or '')}",
                "width": info.get("width"),
                "height": info.get("height"),
                "size": info.get("size"),
                "duration": info.get("duration"),
                "license": meta_value(meta, "LicenseShortName"),
                "license_url": meta_value(meta, "LicenseUrl"),
                "creator": meta_value(meta, "Artist"),
                "attribution": meta_value(meta, "Attribution")
                or meta_value(meta, "Credit"),
            }
        )
    return hits


def relevance(hit: dict[str, Any]) -> int:
    title = clean_title(str(hit.get("title") or "")).lower()
    if any(skip in title for skip in SKIP_TITLE):
        return -1
    strong = sum(1 for word in RELEVANCE_STRONG if word in title)
    weak = sum(1 for word in RELEVANCE_WEAK if word in title)
    return strong * 3 + weak


def keep_hit(
    hit: dict[str, Any],
    *,
    seen: set[str],
    min_width: int,
    max_duration: float,
) -> bool:
    iid = str(hit.get("id") or "")
    if not iid or iid in seen or not hit.get("url"):
        return False
    mime = str(hit.get("mime") or "")
    if mime not in OK_MIME:
        return False
    if not is_ok_license(str(hit.get("license") or "")):
        return False
    width = int(hit.get("width") or 0)
    if min_width and width and width < min_width:
        return False
    duration = hit.get("duration")
    if mime != "image/gif" and duration and float(duration) > max_duration:
        return False
    return relevance(hit) >= 3


def round_robin(buckets: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    index = 0
    while True:
        added = False
        for bucket in buckets:
            if index < len(bucket):
                merged.append(bucket[index])
                added = True
        if not added:
            return merged
        index += 1


def search_slot(
    slot: dict[str, Any],
    *,
    page_size: int,
    min_width: int,
    max_duration: float,
) -> tuple[str, list[dict[str, Any]]]:
    queries = [str(q) for q in (slot.get("queries") or []) if str(q).strip()]
    if not queries:
        queries = [str(slot.get("id") or "gif")]
    last_query = queries[-1]
    seen: set[str] = set()
    buckets: list[list[dict[str, Any]]] = []
    for query in queries:
        last_query = query
        hits: list[dict[str, Any]] = []
        for prefixed in (f"filetype:video {query}",):
            print(f"  search {prefixed!r} ...", flush=True)
            for hit in commons_search(prefixed, page_size=page_size):
                if not keep_hit(
                    hit, seen=seen, min_width=min_width, max_duration=max_duration
                ):
                    continue
                seen.add(str(hit.get("id") or ""))
                hits.append(hit)
            time.sleep(1.2)
        hits.sort(key=relevance, reverse=True)
        buckets.append(hits)
    return last_query, round_robin(buckets)


def clip_start(duration: float | None, clip_seconds: float) -> float:
    if not duration or duration <= clip_seconds + 0.5:
        return 0.0
    start = max(1.0, float(duration) * 0.2)
    if start + clip_seconds > float(duration):
        return max(0.0, float(duration) - clip_seconds)
    return start


def to_gif(
    src: str,
    dest: Path,
    *,
    start: float,
    seconds: float,
    fps: int,
    width: int,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    vf = f"fps={fps},scale={width}:-1:flags=lanczos,format=rgb24"
    starts = [start]
    if start > 0.25:
        starts.append(0.0)
    last_detail = ""
    for ss in starts:
        if dest.exists():
            dest.unlink()
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-user_agent",
            USER_AGENT,
            "-ss",
            f"{ss:.2f}",
            "-t",
            f"{seconds:.2f}",
            "-i",
            src,
            "-an",
            "-vf",
            vf,
            "-loop",
            "0",
            str(dest),
        ]
        proc = subprocess.run(cmd, text=True, capture_output=True)
        if proc.returncode == 0 and dest.is_file() and dest.stat().st_size > 1000:
            return
        last_detail = (proc.stderr or proc.stdout or "").strip()
        dest.unlink(missing_ok=True)
    raise RuntimeError(f"ffmpeg gif failed: {last_detail or dest.name}")


def sniff_gif(path: Path) -> bool:
    data = path.read_bytes()[:6]
    return data.startswith(b"GIF87a") or data.startswith(b"GIF89a")


def load_manifest() -> dict[str, Any]:
    if MANIFEST_PATH.is_file():
        return load_json(MANIFEST_PATH)
    return {
        "source": "wikimedia",
        "license_policy": "CC / public domain (Wikimedia Commons)",
        "inbox": [],
        "suggested": [],
    }


def save_manifest(manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = now_iso()
    write_json(MANIFEST_PATH, manifest)


def image_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(
        p
        for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIX | GIF_SUFFIX
    )


def dest_name(slot_id: str, hit: dict[str, Any]) -> str:
    return f"{slot_id}_{hit.get('id')}_{slug(str(hit.get('title') or 'gif'))}.gif"


def fmt_hit(rank: int, hit: dict[str, Any]) -> str:
    title = clean_title(str(hit.get("title") or "(untitled)"))
    creator = hit.get("creator") or "?"
    mime = str(hit.get("mime") or "?")
    kind = "gif" if mime == "image/gif" else "video→gif"
    dur = hit.get("duration")
    dur_s = f"{float(dur):.1f}s" if dur else "?"
    w = hit.get("width") or "?"
    h = hit.get("height") or "?"
    return (
        f"  {rank}.  {kind:10}  {dur_s:>6}  {w}x{h}  \"{title}\"  [{hit.get('license')}]\n"
        f"      {hit.get('landing')}\n"
        f"      by {creator}"
    )


def entry_from_hit(
    hit: dict[str, Any],
    *,
    folder: str,
    file: str,
    slot: str | None = None,
) -> dict[str, Any]:
    return {
        "slot": slot,
        "folder": folder,
        "commons_pageid": hit.get("id"),
        "name": clean_title(str(hit.get("title") or "")),
        "creator": hit.get("creator"),
        "license": hit.get("license"),
        "license_url": hit.get("license_url"),
        "url": hit.get("landing"),
        "media_url": hit.get("url"),
        "attribution": hit.get("attribution"),
        "width": hit.get("width"),
        "height": hit.get("height"),
        "duration": hit.get("duration"),
        "source_mime": hit.get("mime"),
        "filetype": "gif",
        "file": file,
        "fetched_at": now_iso(),
    }


def upsert(manifest: dict[str, Any], key: str, entry: dict[str, Any]) -> None:
    rows = [r for r in (manifest.get(key) or []) if r.get("file") != entry["file"]]
    rows.append(entry)
    rows.sort(key=lambda r: str(r.get("file") or ""))
    manifest[key] = rows


def cmd_list(
    slots: list[dict[str, Any]],
    *,
    page_size: int,
    min_width: int,
    max_duration: float,
    only: str | None,
) -> int:
    for i, slot in enumerate(slots):
        if only and slot["id"] != only:
            continue
        if i:
            time.sleep(0.4)
        query, results = search_slot(
            slot,
            page_size=page_size,
            min_width=min_width,
            max_duration=max_duration,
        )
        print(f"[{slot['id']}] {slot['label']}  query={query!r}  ({len(results)} shown)")
        print(f"  use on: {slot['use_on']}")
        if not results:
            print("  (no GIF / video hits)")
            print(f"  {web_search_url(query or slot['id'])}\n")
            continue
        for rank, hit in enumerate(results[:page_size], start=1):
            print(fmt_hit(rank, hit))
        print()
    return 0


def cmd_suggest(
    slots: list[dict[str, Any]],
    *,
    page_size: int,
    min_width: int,
    max_duration: float,
    clip_seconds: float,
    gif_fps: int,
    gif_width: int,
    per_slot: int,
    only: str | None,
    force: bool,
) -> int:
    SUGGEST_DIR.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()
    manifest["source"] = "wikimedia"
    have_ids = {
        str(r.get("commons_pageid") or r.get("openverse_id") or "")
        for r in (manifest.get("suggested") or [])
        if r.get("commons_pageid") or r.get("openverse_id")
    }
    saved = 0
    for i, slot in enumerate(slots):
        if only and slot["id"] != only:
            continue
        if i:
            time.sleep(0.4)
        _query, results = search_slot(
            slot,
            page_size=page_size,
            min_width=min_width,
            max_duration=max_duration,
        )
        picked = 0
        seen_titles: set[str] = set()
        for hit in results:
            if picked >= per_slot:
                break
            title_key = slug(str(hit.get("title") or ""))[:18]
            if title_key in seen_titles:
                continue
            iid = str(hit.get("id") or "")
            dest = SUGGEST_DIR / dest_name(str(slot["id"]), hit)
            if (dest.is_file() or iid in have_ids) and not force:
                print(f"[{slot['id']}] have {dest.name}")
                picked += 1
                continue
            mime = str(hit.get("mime") or "")
            try:
                if mime == "image/gif":
                    http_download(clip_source_url(hit), dest)
                    if not sniff_gif(dest):
                        dest.unlink(missing_ok=True)
                        print(f"[{slot['id']}] skip {hit.get('title')} (not a gif)")
                        continue
                else:
                    start = clip_start(
                        float(hit["duration"]) if hit.get("duration") else None,
                        clip_seconds,
                    )
                    with tempfile.TemporaryDirectory(prefix="gif-") as tmp:
                        raw = Path(tmp) / "clip.webm"
                        http_download(clip_source_url(hit), raw)
                        to_gif(
                            str(raw),
                            dest,
                            start=start,
                            seconds=clip_seconds,
                            fps=gif_fps,
                            width=gif_width,
                        )
                    if not sniff_gif(dest):
                        dest.unlink(missing_ok=True)
                        print(f"[{slot['id']}] skip {hit.get('title')} (ffmpeg not gif)")
                        continue
            except RuntimeError as exc:
                dest.unlink(missing_ok=True)
                print(f"[{slot['id']}] skip {clean_title(str(hit.get('title') or ''))}: {exc}")
                continue
            entry = entry_from_hit(
                hit,
                folder="suggested",
                file=str(dest.relative_to(ROOT)),
                slot=str(slot["id"]),
            )
            upsert(manifest, "suggested", entry)
            have_ids.add(iid)
            seen_titles.add(title_key)
            kind = "gif" if mime == "image/gif" else "video→gif"
            print(f"[{slot['id']}] {dest.name}  ({kind})")
            print(f"         {entry['name']}  [{entry.get('license')}]")
            print(f"         {entry['url']}")
            picked += 1
            saved += 1
            time.sleep(2.0)
        if picked == 0:
            print(f"[{slot['id']}] no GIF hits")
    save_manifest(manifest)
    print(f"\nsuggested {saved} file(s) → {SUGGEST_DIR.relative_to(ROOT)}/")
    return 0


def cmd_adopt(
    path: Path,
    *,
    image_id: str | None,
    url: str | None,
) -> int:
    if not path.is_file():
        raise RuntimeError(f"file not found: {path}")
    if path.suffix.lower() not in GIF_SUFFIX:
        raise RuntimeError(f"not a gif: {path.name}")
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    dest = INBOX_DIR / path.name
    if path.resolve() != dest.resolve():
        shutil.copy2(path, dest)
    hit = {
        "id": image_id,
        "title": path.stem,
        "creator": None,
        "license": None,
        "license_url": None,
        "landing": url,
        "url": url,
        "attribution": None,
        "width": None,
        "height": None,
        "duration": None,
        "mime": "image/gif",
    }
    entry = entry_from_hit(
        hit,
        folder="inbox",
        file=str(dest.relative_to(ROOT)),
    )
    manifest = load_manifest()
    upsert(manifest, "inbox", entry)
    save_manifest(manifest)
    print(f"inbox ← {dest.name}")
    if entry.get("url"):
        print(f"         {entry['url']}")
    return 0


def cmd_status(slots: list[dict[str, Any]]) -> int:
    inbox = image_files(INBOX_DIR)
    suggested = image_files(SUGGEST_DIR)
    print("assets/gifs/inbox/     ← drop your GIFs here")
    if inbox:
        for p in inbox:
            print(f"  {p.name}")
    else:
        print("  (empty)")
    print("\nassets/gifs/suggested/ ← Commons GIFs / video clips")
    if suggested:
        for p in suggested:
            kind = "gif" if p.suffix.lower() in GIF_SUFFIX else "not-gif"
            print(f"  {p.name}  ({kind})")
    else:
        print("  (empty — run: python fetch_gifs.py --suggest)")
    print("\nsearch recipes:")
    for slot in slots:
        print(f"  {slot['id']:10}  {slot['label']:28}  {slot['use_on']}")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Inbox + suggested Wikimedia Commons GIF pack"
    )
    p.add_argument("--list", action="store_true", help="Show GIF / video matches")
    p.add_argument(
        "--suggest",
        action="store_true",
        help="Download GIFs / clip videos into suggested/",
    )
    p.add_argument("--fetch", action="store_true", help="Alias for --suggest")
    p.add_argument("--status", action="store_true", help="Show inbox + suggested")
    p.add_argument("--adopt", metavar="PATH", help="Copy a local GIF into inbox/")
    p.add_argument("--slot", default=None, help="Only this suggestion recipe")
    p.add_argument("--id", default=None, dest="image_id", help="Commons page id")
    p.add_argument("--url", default=None)
    p.add_argument("--force", action="store_true")
    p.add_argument("--page-size", type=int, default=None)
    p.add_argument("--per-slot", type=int, default=None, help="Picks per recipe")
    return p.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()
    cfg = load_json(SLOTS_PATH)
    slots = list(cfg.get("slots") or [])
    if not slots:
        print("error: assets/gifs/slots.json has no slots", file=sys.stderr)
        return 1
    min_width = int(cfg.get("min_width") or 0)
    max_duration = float(cfg.get("max_duration") or 240)
    clip_seconds = float(cfg.get("clip_seconds") or 2.5)
    gif_fps = int(cfg.get("gif_fps") or 12)
    gif_width = int(cfg.get("gif_width") or 480)
    page_size = int(args.page_size or cfg.get("page_size") or 10)
    per_slot = int(args.per_slot or cfg.get("per_slot") or 4)
    known = {str(s["id"]) for s in slots}
    if args.slot and args.slot not in known:
        print(
            f"error: unknown slot {args.slot!r} — {', '.join(sorted(known))}",
            file=sys.stderr,
        )
        return 1

    try:
        if args.adopt:
            return cmd_adopt(
                Path(args.adopt).expanduser(),
                image_id=args.image_id,
                url=args.url,
            )
        if args.list:
            return cmd_list(
                slots,
                page_size=page_size,
                min_width=min_width,
                max_duration=max_duration,
                only=args.slot,
            )
        if args.suggest or args.fetch:
            return cmd_suggest(
                slots,
                page_size=page_size,
                min_width=min_width,
                max_duration=max_duration,
                clip_seconds=clip_seconds,
                gif_fps=gif_fps,
                gif_width=gif_width,
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
