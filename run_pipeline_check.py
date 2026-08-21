#!/usr/bin/env python3
"""Run staged Twitch + Riot checks and append results to a durable log."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from env_loader import load_dotenv


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG_PATH = LOG_DIR / "pipeline_checks.jsonl"
LATEST_PATH = LOG_DIR / "pipeline_checks_latest.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def append_log(entry: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    LATEST_PATH.write_text(json.dumps(entry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def run_step(name: str, fn) -> dict[str, Any]:
    started = utc_now()
    step: dict[str, Any] = {
        "name": name,
        "startedAt": started,
        "ok": False,
    }
    try:
        detail = fn()
        step["ok"] = True
        step["detail"] = detail
    except Exception as exc:
        step["ok"] = False
        step["error"] = str(exc)
        step["traceback"] = traceback.format_exc(limit=4)
    step["finishedAt"] = utc_now()
    status = "PASS" if step["ok"] else "FAIL"
    print(f"[{status}] {name}")
    if step["ok"]:
        summary = step.get("detail", {})
        if isinstance(summary, dict) and "summary" in summary:
            print(f"       {summary['summary']}")
        else:
            print(f"       {summary}")
    else:
        print(f"       {step.get('error')}")
    print()
    return step


def step_env() -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    riot_id = (os.getenv("RIOT_ID") or "").strip()
    channel = (os.getenv("TWITCH_CHANNEL") or "").strip()
    region = (os.getenv("RIOT_REGION") or "americas").strip()
    missing = [
        name
        for name, ok in (
            ("RIOT_API_KEY", bool(os.getenv("RIOT_API_KEY"))),
            ("RIOT_ID", bool(riot_id)),
            ("TWITCH_CLIENT_ID", bool(os.getenv("TWITCH_CLIENT_ID"))),
            ("TWITCH_CLIENT_SECRET", bool(os.getenv("TWITCH_CLIENT_SECRET"))),
            ("TWITCH_CHANNEL", bool(channel)),
        )
        if not ok
    ]
    if missing:
        raise RuntimeError(f"missing env: {', '.join(missing)}")
    return {
        "summary": f"env ok · channel={channel} · riotId={riot_id} · region={region}",
        "present": {
            "RIOT_API_KEY": True,
            "TWITCH_CLIENT_ID": True,
            "TWITCH_CLIENT_SECRET": True,
            "RIOT_ID": riot_id,
            "TWITCH_CHANNEL": channel,
            "RIOT_REGION": region,
        },
    }


def step_twitch_list() -> dict[str, Any]:
    # Import after env load so credentials are available.
    load_dotenv(ROOT / ".env")
    from list_vods import get_app_access_token, get_user_id, list_archives

    client_id = os.environ["TWITCH_CLIENT_ID"]
    client_secret = os.environ["TWITCH_CLIENT_SECRET"]
    channel = os.environ["TWITCH_CHANNEL"]

    token = get_app_access_token(client_id, client_secret)
    user = get_user_id(client_id, token, channel)
    videos = list_archives(client_id, token, str(user["id"]), limit=3)
    if not videos:
        raise RuntimeError(f"no archive VODs returned for {channel}")

    slim = [
        {
            "id": v.get("id"),
            "title": v.get("title"),
            "url": v.get("url"),
            "created_at": v.get("created_at"),
            "duration": v.get("duration"),
        }
        for v in videos
    ]
    return {
        "summary": f"{len(slim)} VOD(s) for {user.get('display_name') or channel}",
        "videos": slim,
    }


def step_riot_index() -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    out = ROOT / "logs" / "step2_events.json"
    cmd = [
        sys.executable,
        str(ROOT / "lol-indexer" / "lol_indexer.py"),
        "--count",
        "1",
        "--output",
        str(out),
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT / "lol-indexer"),
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(err or f"lol_indexer exited {proc.returncode}")

    data = json.loads(out.read_text(encoding="utf-8"))
    matches = data.get("matches") or []
    if not matches:
        raise RuntimeError("Riot resolved but returned 0 matches")
    m0 = matches[0]
    return {
        "summary": (
            f"{data.get('player', {}).get('riotId')} · "
            f"{m0.get('matchId')} · {m0.get('champion')} "
            f"{m0.get('kills')}/{m0.get('deaths')}/{m0.get('assists')} · "
            f"{len(m0.get('events') or [])} events"
        ),
        "output": str(out),
        "matchId": m0.get("matchId"),
        "eventCount": len(m0.get("events") or []),
        "stdoutTail": "\n".join((proc.stdout or "").strip().splitlines()[-12:]),
    }


def step_vod_map(vod_dir: Path) -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    if not vod_dir.is_dir():
        raise RuntimeError(f"VOD dir missing: {vod_dir}")
    meta = vod_dir / "metadata.json"
    if not meta.is_file():
        raise RuntimeError(f"missing metadata.json in {vod_dir} (ingest first)")

    out = vod_dir / "lol_events.json"
    cmd = [
        sys.executable,
        str(ROOT / "lol-indexer" / "lol_indexer.py"),
        "--vod-dir",
        str(vod_dir),
        "--output",
        str(out),
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT / "lol-indexer"),
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(err or f"lol_indexer --vod-dir exited {proc.returncode}")

    data = json.loads(out.read_text(encoding="utf-8"))
    matches = data.get("matches") or []
    event_total = sum(len(m.get("events") or []) for m in matches)
    with_vod_time = sum(
        1
        for m in matches
        for e in (m.get("events") or [])
        if e.get("vodTime") is not None
    )
    if not matches:
        # Soft signal: pipeline worked, but no League games overlapped this VOD.
        return {
            "summary": (
                f"mapped VOD {data.get('vod', {}).get('datasetId')} · "
                "0 matches in window (soft pass — try a newer VOD)"
            ),
            "softPass": True,
            "output": str(out),
            "matchCount": 0,
            "eventCount": 0,
            "eventsWithVodTime": 0,
            "stdoutTail": "\n".join((proc.stdout or "").strip().splitlines()[-16:]),
        }

    sample = None
    for m in matches:
        for e in m.get("events") or []:
            if e.get("type") in {"KILL", "DEATH", "ASSIST"} and e.get("vodTime"):
                sample = {
                    "matchId": m.get("matchId"),
                    "champion": m.get("champion"),
                    "type": e.get("type"),
                    "gameTime": e.get("gameTime"),
                    "vodTime": e.get("vodTime"),
                    "clipStart": e.get("clipStart"),
                    "clipEnd": e.get("clipEnd"),
                }
                break
        if sample:
            break

    return {
        "summary": (
            f"{len(matches)} match(es) · {event_total} events · "
            f"{with_vod_time} with vodTime"
        ),
        "output": str(out),
        "matchCount": len(matches),
        "eventCount": event_total,
        "eventsWithVodTime": with_vod_time,
        "sampleEvent": sample,
        "stdoutTail": "\n".join((proc.stdout or "").strip().splitlines()[-16:]),
    }


def main() -> int:
    load_dotenv(ROOT / ".env")
    run_id = utc_now().replace(":", "").replace("-", "")
    print(f"Pipeline check {run_id}\n")

    steps: list[dict[str, Any]] = []
    steps.append(run_step("0_env", step_env))
    if not steps[-1]["ok"]:
        report = _finalize(run_id, steps)
        print(f"Wrote {LOG_PATH}")
        print(f"Latest: {LATEST_PATH}")
        return 1

    steps.append(run_step("1_twitch_list_vods", step_twitch_list))
    steps.append(run_step("2_riot_index_one_match", step_riot_index))

    # Prefer existing ingested dataset; fall back to newest Twitch VOD id for messaging.
    existing = sorted((ROOT / "data").glob("*/metadata.json"))
    if existing:
        vod_dir = existing[-1].parent
        from dataset_paths import find_dataset_dir

        preferred = find_dataset_dir(ROOT / "data", "2833454760")
        if preferred and (preferred / "metadata.json").is_file():
            vod_dir = preferred
        steps.append(run_step("3_vod_map", lambda: step_vod_map(vod_dir)))
    else:
        steps.append(
            run_step(
                "3_vod_map",
                lambda: (_ for _ in ()).throw(
                    RuntimeError(
                        "no ingested VOD under data/; run ingest_vod.py on a URL "
                        "from step 1, then re-run this check"
                    )
                ),
            )
        )

    report = _finalize(run_id, steps)
    passed = sum(1 for s in steps if s["ok"])
    print(f"Result: {passed}/{len(steps)} steps passed")
    print(f"Log append: {LOG_PATH}")
    print(f"Latest report: {LATEST_PATH}")

    # Step 3 soft-pass (0 matches) still counts as ok=True.
    hard_fail = any(not s["ok"] for s in steps)
    return 1 if hard_fail else 0


def _finalize(run_id: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    report = {
        "runId": run_id,
        "generatedAt": utc_now(),
        "ok": all(s["ok"] for s in steps),
        "passed": sum(1 for s in steps if s["ok"]),
        "total": len(steps),
        "steps": steps,
    }
    append_log(report)
    return report


if __name__ == "__main__":
    sys.exit(main())
