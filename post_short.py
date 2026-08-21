#!/usr/bin/env python3
"""Upload finished shorts: a private YouTube video and/or a TikTok draft.

Nothing is published. YouTube lands as private and TikTok lands in your inbox, so
the last look and the actual post still happen on your phone.

  python post_short.py --login youtube
  python post_short.py --dataset-id 2849217240 --from-picks --youtube --tiktok
  python post_short.py --input data/{day}_{vod}/.../post/gam08_..._portrait_music.mp4 --youtube
  python post_short.py --dataset-id 2849217240 --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from dataset_paths import find_dataset_dir, game_only_matches
from env_loader import load_dotenv
from posting import meta
from posting import tiktok as tiktok_api
from posting import youtube as youtube_api

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

PLATFORMS = ("youtube", "tiktok")


def run_print(msg: str) -> None:
    print(msg, flush=True)


def progress_printer(tag: str, name: str):
    seen = {"pct": -1}

    def on_progress(pct: int) -> None:
        step = pct - (pct % 20)
        if step <= seen["pct"] or pct <= 0:
            return
        seen["pct"] = step
        run_print(f"[{tag}] {name} {min(pct, 100)}%")

    return on_progress


def resolve_dataset(args: argparse.Namespace) -> Path | None:
    if args.dataset_dir is not None:
        return args.dataset_dir.resolve()
    if args.dataset_id:
        found = find_dataset_dir(args.output_root, args.dataset_id)
        return (found or (args.output_root / args.dataset_id)).resolve()
    return None


def dataset_for_video(video: Path) -> Path | None:
    """Walk up to the data/{day}_{vod}/ folder so titles.json can be found."""
    for parent in video.resolve().parents:
        if (parent / "lol_clips").is_dir() or (parent / "metadata.json").is_file():
            return parent
    return None


def collect_videos(args: argparse.Namespace, dataset: Path | None) -> list[Path]:
    if args.input is not None:
        inp = args.input.resolve()
        if inp.is_dir():
            found = [p for p in sorted(inp.glob("*.mp4")) if meta.is_postable(p)]
            found += [p for p in sorted(inp.glob("post/*.mp4")) if meta.is_postable(p)]
            if args.only:
                found = [p for p in found if game_only_matches(p.name, args.only)]
            return found
        return [inp]
    if dataset is None:
        return []
    if not dataset.is_dir():
        raise FileNotFoundError(dataset)
    return meta.discover_shorts(dataset, only=str(args.only or ""), picks_only=bool(args.from_picks))


def upload_youtube(video: Path, record: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    result = youtube_api.upload(
        video,
        title=record["title"],
        description=record.get("description") or "",
        tags=list(record.get("hashtags") or []),
        category_id=str(args.category_id),
        privacy=str(args.privacy),
        made_for_kids=bool(args.made_for_kids),
        on_progress=progress_printer("youtube", video.stem),
    )
    run_print(f"[youtube] {result['url']} ({args.privacy}) — finish it in Studio")
    return {"status": "uploaded", "uploaded_at": meta.now_stamp(), **result}


def upload_tiktok(video: Path, record: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    result = tiktok_api.upload_draft(
        video,
        on_progress=progress_printer("tiktok", video.stem),
        timeout=float(args.tiktok_timeout),
    )
    run_print(f"[tiktok] draft {result.get('status')} — open the TikTok inbox to finish it")
    run_print(f"[tiktok] caption to paste: {record['title']}")
    return {"status": str(result.get("status") or "PROCESSING_UPLOAD"), "uploaded_at": meta.now_stamp(), **result}


def post_one(video: Path, *, dataset: Path | None, args: argparse.Namespace) -> dict[str, Any]:
    record = meta.build_record(
        video,
        dataset=dataset,
        title=args.title,
        description=args.description,
        hashtags=[t for t in str(args.hashtags or "").split(",") if t.strip()] or None,
    )
    run_print(f"[post] {video.name}")
    run_print(f"[post] title: {record['title']} ({record['titleSource']})")

    if args.dry_run or args.meta_only:
        meta.write_sidecar(video, record)
        return {"video": str(video), "title": record["title"], "skipped": True, "record": record}

    failures: list[str] = []
    for platform in PLATFORMS:
        if not getattr(args, platform):
            continue
        if meta.already_posted(record, platform) and not args.force:
            state = meta.platform_state(record, platform)
            run_print(f"[{platform}] already posted ({state.get('videoId') or state.get('publishId')}); --force to redo")
            continue
        try:
            if platform == "youtube":
                record["youtube"] = upload_youtube(video, record, args)
            else:
                record["tiktok"] = upload_tiktok(video, record, args)
        except Exception as exc:
            message = str(exc)
            run_print(f"[{platform}] failed: {message}")
            record[platform] = {
                **meta.platform_state(record, platform),
                "status": "failed",
                "error": message,
                "attempted_at": meta.now_stamp(),
            }
            failures.append(platform)
        meta.write_sidecar(video, record)

    meta.write_sidecar(video, record)
    return {
        "video": str(video),
        "title": record["title"],
        "failed": failures,
        "youtube": record.get("youtube") or {},
        "tiktok": record.get("tiktok") or {},
    }


def do_login(target: str) -> int:
    if target == "youtube":
        result = youtube_api.login()
    else:
        result = tiktok_api.login()
    run_print(f"[{target}] authorized · token {result.get('tokenFile')}")
    if target == "tiktok" and not result.get("uploadScope"):
        run_print(f"[tiktok] warning: {tiktok_api.SCOPE} not granted. {tiktok_api.REVIEW_HINT}")
        return 1
    return 0


def print_status() -> int:
    print(json.dumps({"youtube": youtube_api.status(), "tiktok": tiktok_api.status()}, indent=2))
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Upload rendered shorts as a private YouTube video and/or a TikTok draft."
    )
    p.add_argument("--input", type=Path, help="A *_portrait_music.mp4, or a folder of them")
    p.add_argument("--dataset-id")
    p.add_argument("--dataset-dir", type=Path)
    p.add_argument("--output-root", type=Path, default=DATA)
    p.add_argument("--only", default="", help="Single game, e.g. g08 or gam08")
    p.add_argument(
        "--from-picks",
        action="store_true",
        help="Only lol_compilations_picks_portrait/",
    )
    p.add_argument("--youtube", action="store_true", help="Upload as a private YouTube video")
    p.add_argument("--tiktok", action="store_true", help="Upload to the TikTok draft inbox")
    p.add_argument("--title", help="Override the title for every upload in this run")
    p.add_argument("--description", help="Override the description")
    p.add_argument("--hashtags", default="", help="Comma-separated, no # needed")
    p.add_argument(
        "--privacy",
        default="private",
        choices=youtube_api.PRIVACY_LEVELS,
        help="YouTube privacy (default private so you finish in Studio)",
    )
    p.add_argument("--category-id", default=youtube_api.GAMING_CATEGORY_ID, help="YouTube category (20 = Gaming)")
    p.add_argument(
        "--made-for-kids",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="YouTube selfDeclaredMadeForKids",
    )
    p.add_argument("--tiktok-timeout", type=float, default=300.0, help="Seconds to wait for TikTok processing")
    p.add_argument("--limit", type=int, default=0, help="Stop after N videos")
    p.add_argument("--meta-only", action="store_true", help="Write .post.json sidecars, upload nothing")
    p.add_argument("--dry-run", action="store_true", help="Show titles and write sidecars, upload nothing")
    p.add_argument("--force", action="store_true", help="Re-upload even if the sidecar says it is done")
    p.add_argument("--login", choices=PLATFORMS, help="Authorize a platform and exit")
    p.add_argument("--status", action="store_true", help="Print credential / auth state and exit")
    return p.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()
    try:
        if args.login:
            return do_login(args.login)
        if args.status:
            return print_status()

        wants_upload = bool(args.youtube or args.tiktok)
        if not wants_upload and not (args.dry_run or args.meta_only):
            print(
                "Pass --youtube and/or --tiktok (or --dry-run / --meta-only).",
                file=sys.stderr,
            )
            return 2

        dataset = resolve_dataset(args)
        videos = collect_videos(args, dataset)
        if not videos:
            raise FileNotFoundError("No *_portrait_music.mp4 to post. Run Add music first.")
        if args.limit and args.limit > 0:
            videos = videos[: args.limit]

        results: list[dict[str, Any]] = []
        failed = 0
        for video in videos:
            owner = dataset or dataset_for_video(video)
            result = post_one(video, dataset=owner, args=args)
            if result.get("failed"):
                failed += 1
            results.append(result)

        print(
            json.dumps(
                {
                    "status": "ok" if not failed else "partial",
                    "count": len(results),
                    "failed": failed,
                    "results": results,
                },
                indent=2,
            )
        )
        return 1 if failed else 0
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Post failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
