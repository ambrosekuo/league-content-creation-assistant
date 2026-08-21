#!/usr/bin/env python3
"""Send one mp4 to your TikTok inbox as a draft.

Nothing is published: TikTok drops the file in your inbox and you finish the
caption, sounds and cover in the app. This is the single-file companion to
post_short.py, handy for testing the credentials before the pipeline uses them.

  python3 upload_tiktok.py --login
  python3 upload_tiktok.py --check
  python3 upload_tiktok.py path/to/video.mp4

Credentials live in secrets/ (gitignored):
  secrets/tiktok_client.json   {"client_key": "...", "client_secret": "..."}
  secrets/tiktok_token.json    written by --login, refreshed automatically
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from env_loader import load_dotenv
from posting import meta
from posting import tiktok as tiktok_api


def progress_printer(name: str):
    seen = {"pct": -1}

    def on_progress(pct: int) -> None:
        step = pct - (pct % 20)
        if step <= seen["pct"] or pct <= 0:
            return
        seen["pct"] = step
        print(f"[tiktok] {name} {min(pct, 100)}%", flush=True)

    return on_progress


def do_login(args: argparse.Namespace) -> int:
    result = tiktok_api.login(
        paste=bool(args.paste),
        open_browser=not args.no_browser,
        timeout=float(args.login_timeout),
    )
    print(f"[tiktok] authorized as {result.get('openId') or 'unknown open_id'}", flush=True)
    print(f"[tiktok] token   {result.get('tokenFile')}", flush=True)
    print(f"[tiktok] scopes  {', '.join(result.get('scopes') or []) or 'none returned'}", flush=True)
    if not result.get("uploadScope"):
        print(f"[tiktok] warning: {tiktok_api.SCOPE} was not granted. {tiktok_api.REVIEW_HINT}", flush=True)
        return 1
    return 0


def do_check() -> int:
    report = tiktok_api.preflight()
    print(json.dumps(report, indent=2))
    for blocker in report["blockers"]:
        print(f"[tiktok] blocked: {blocker}", file=sys.stderr)
    return 0 if report["ok"] else 1


def caption_for(video: Path, args: argparse.Namespace) -> str:
    """Reuse the pipeline's title logic when the file is a known short."""
    if args.caption:
        return str(args.caption)
    if not meta.is_postable(video):
        return video.stem
    record = meta.build_record(video)
    return str(record.get("title") or video.stem)


def do_upload(video: Path, args: argparse.Namespace) -> int:
    if not video.is_file():
        raise FileNotFoundError(video)

    caption = caption_for(video, args)
    size_mb = video.stat().st_size / (1024 * 1024)
    chunk, chunks = tiktok_api.chunk_plan(video.stat().st_size)
    print(f"[tiktok] {video.name} ({size_mb:.1f} MB, {chunks} chunk(s) of {chunk // (1024 * 1024)} MB)", flush=True)
    print(f"[tiktok] caption to paste in the app: {caption}", flush=True)

    if args.dry_run:
        print("[tiktok] dry run, nothing uploaded", flush=True)
        return 0

    result = tiktok_api.upload_draft(
        video,
        on_progress=progress_printer(video.stem),
        wait=not args.no_wait,
        timeout=float(args.timeout),
        require_scope=args.require_scope,
    )
    print(f"[tiktok] draft {result.get('status')} — open the TikTok inbox to finish it", flush=True)

    # Keep the viewer's Post tab in sync when this is a pipeline export.
    if meta.is_postable(video):
        record = meta.build_record(video, title=args.caption)
        record["tiktok"] = {
            "status": str(result.get("status") or "PROCESSING_UPLOAD"),
            "uploaded_at": meta.now_stamp(),
            **result,
        }
        meta.write_sidecar(video, record)

    print(json.dumps({"status": "ok", "video": str(video), "caption": caption, "tiktok": result}, indent=2))
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Upload one mp4 to the TikTok draft inbox.")
    p.add_argument("video", nargs="?", type=Path, help="Path to the mp4")
    p.add_argument("--login", action="store_true", help="Authorize a TikTok account and exit")
    p.add_argument("--status", action="store_true", help="Print credential / token state and exit")
    p.add_argument("--check", action="store_true", help="Verify creds, token and video.upload scope")
    p.add_argument("--caption", help="Caption to suggest (default: the pipeline title)")
    p.add_argument("--paste", action="store_true", help="Paste the redirected URL instead of a local callback")
    p.add_argument("--no-browser", action="store_true", help="Do not open the consent URL automatically")
    p.add_argument("--login-timeout", type=float, default=300.0, help="Seconds to wait for the callback")
    p.add_argument("--timeout", type=float, default=300.0, help="Seconds to wait for TikTok processing")
    p.add_argument("--no-wait", action="store_true", help="Return once bytes are sent, skip status polling")
    p.add_argument(
        "--require-scope",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Refuse to upload when the token lacks video.upload (default on)",
    )
    p.add_argument("--dry-run", action="store_true", help="Show the plan and caption, upload nothing")
    return p.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()
    try:
        if args.login:
            return do_login(args)
        if args.status:
            print(json.dumps(tiktok_api.status(), indent=2))
            return 0
        if args.check:
            return do_check()
        if args.video is None:
            print("Pass a video path, or --login / --check.", file=sys.stderr)
            return 2
        return do_upload(args.video.resolve(), args)
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"TikTok upload failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
