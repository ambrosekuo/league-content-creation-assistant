#!/usr/bin/env python3
"""CLI: retroactively index LoL matches into clip-ready timestamps."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from models import (
    IndexResult,
    MatchSummary,
    add_game_bookend_events,
    apply_video_offsets,
    extract_player_events,
    parse_iso_datetime,
    parse_riot_id,
    to_epoch_seconds,
)
from riot_api import (
    RiotAPI,
    RiotAPIError,
    RiotAuthError,
    RiotNotFoundError,
)

# Allow `python lol_indexer.py` from this folder while loading ../.env
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from env_loader import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(path=None):  # type: ignore[misc]
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Index League of Legends match timelines for a Riot ID and "
            "emit clip-ready kill/death/assist timestamps."
        )
    )
    parser.add_argument(
        "--riot-id",
        default=None,
        help='Riot ID "GameName#TAG" (default: RIOT_ID env, e.g. lolAmbrosek#twtv)',
    )
    parser.add_argument(
        "--count",
        type=int,
        default=10,
        help="Number of recent matches to fetch (default: 10, max: 100)",
    )
    parser.add_argument(
        "--start-time",
        default=None,
        help="Optional ISO-8601 window start (e.g. 2026-08-09T13:00:00-04:00)",
    )
    parser.add_argument(
        "--end-time",
        default=None,
        help="Optional ISO-8601 window end",
    )
    parser.add_argument(
        "--output",
        default="events.json",
        help="Output JSON path (default: events.json)",
    )
    parser.add_argument(
        "--region",
        default=None,
        help="Riot regional routing (default: RIOT_REGION env, else americas)",
    )
    parser.add_argument(
        "--vod-dir",
        type=Path,
        default=None,
        help=(
            "Ingested Twitch VOD folder (e.g. ../data/2833454760). "
            "Uses metadata.json timestamp as the VOD timeline start."
        ),
    )
    parser.add_argument(
        "--obs-start",
        default=None,
        help="OBS recording start (ISO-8601) for absolute clip offsets",
    )
    parser.add_argument(
        "--pre-roll",
        type=int,
        default=15,
        help="Seconds before each event for clip start (default: 15)",
    )
    parser.add_argument(
        "--post-roll",
        type=int,
        default=10,
        help="Seconds after each event for clip end (default: 10)",
    )
    return parser


def load_vod_timeline(vod_dir: Path) -> dict[str, Any]:
    """Load VOD start/duration from an ingested dataset folder."""
    vod_dir = vod_dir.resolve()
    if not vod_dir.is_dir():
        raise FileNotFoundError(f"VOD directory not found: {vod_dir}")

    metadata_path = vod_dir / "metadata.json"
    info_path = vod_dir / "source.info.json"
    ingest_path = vod_dir / "ingest.json"
    if not metadata_path.is_file() and info_path.is_file():
        # Cloud resume may only have yt-dlp's source.info.json.
        metadata_path.write_bytes(info_path.read_bytes())
    if not metadata_path.is_file():
        raise FileNotFoundError(
            f"missing {metadata_path.name}; ingest the VOD first with ingest_vod.py"
        )

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    timestamp = metadata.get("timestamp")
    if timestamp is None:
        raise ValueError(f"{metadata_path} has no 'timestamp' (VOD start epoch)")

    start = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
    duration = metadata.get("duration")
    if duration is None and ingest_path.is_file():
        ingest = json.loads(ingest_path.read_text(encoding="utf-8"))
        duration = ingest.get("duration_seconds")

    duration_seconds = float(duration) if duration is not None else None
    end = (
        start + timedelta(seconds=duration_seconds)
        if duration_seconds is not None
        else None
    )

    from dataset_paths import vod_id_from_dir_name

    vod_id = vod_id_from_dir_name(vod_dir.name)
    return {
        "datasetId": metadata.get("id") or vod_id,
        "title": metadata.get("title"),
        "uploader": metadata.get("uploader") or metadata.get("uploader_id"),
        "url": metadata.get("webpage_url")
        or f"https://www.twitch.tv/videos/{vod_id}",
        "start": start,
        "end": end,
        "durationSeconds": duration_seconds,
        "dir": str(vod_dir),
    }


def find_participant(match: dict[str, Any], puuid: str) -> dict[str, Any] | None:
    participants = (match.get("info") or {}).get("participants") or []
    for participant in participants:
        if participant.get("puuid") == puuid:
            return participant
    return None


def match_in_window(
    game_start_ms: int,
    start_dt: datetime | None,
    end_dt: datetime | None,
    *,
    game_duration_seconds: float | None = None,
) -> bool:
    """
    True if the match overlaps the optional [start, end] window.

    Uses overlap (not start-only) so games already in progress when a VOD
    begins are still indexed.
    """
    if start_dt is None and end_dt is None:
        return True
    game_start = game_start_ms / 1000.0
    duration = float(game_duration_seconds or 0.0)
    if duration < 0:
        duration = 0.0
    game_end = game_start + duration
    window_start = start_dt.timestamp() if start_dt is not None else None
    window_end = end_dt.timestamp() if end_dt is not None else None
    if window_end is not None and game_start > window_end:
        return False
    if window_start is not None and game_end < window_start:
        return False
    return True


def find_lane_opponent_champion(
    match: dict[str, Any],
    participant: dict[str, Any],
) -> str | None:
    """Enemy champ in the same role (TOP vs TOP, MIDDLE vs MIDDLE, …)."""
    my_pos = str(participant.get("teamPosition") or "").strip().upper()
    my_team = participant.get("teamId")
    if not my_pos or my_team is None:
        return None
    info = match.get("info") or {}
    for other in info.get("participants") or []:
        if other.get("teamId") == my_team:
            continue
        if str(other.get("teamPosition") or "").strip().upper() != my_pos:
            continue
        name = other.get("championName")
        if name:
            return str(name)
    return None


def index_matches(
    api: RiotAPI,
    riot_id: str,
    puuid: str,
    match_ids: list[str],
    start_dt: datetime | None,
    end_dt: datetime | None,
    video_start: datetime | None,
    video_kind: str,
    pre_roll: int,
    post_roll: int,
) -> IndexResult:
    result = IndexResult(riot_id=riot_id, puuid=puuid)
    summaries: list[MatchSummary] = []

    for match_id in match_ids:
        try:
            match = api.get_match(match_id)
        except RiotNotFoundError:
            print(f"warning: match not found: {match_id}", file=sys.stderr)
            continue
        except RiotAPIError as exc:
            print(f"warning: failed to fetch match {match_id}: {exc}", file=sys.stderr)
            continue

        info = match.get("info") or {}
        game_start = info.get("gameStartTimestamp") or info.get("gameCreation")
        if game_start is None:
            print(f"warning: match {match_id} missing game start timestamp", file=sys.stderr)
            continue

        game_start_ms = int(game_start)
        # Duration needed early for overlap filtering (games already running at VOD start).
        duration = info.get("gameDuration")
        if duration is not None and int(duration) > 100_000:
            duration_seconds = int(duration) // 1000
        else:
            duration_seconds = int(duration or 0)

        if not match_in_window(
            game_start_ms,
            start_dt,
            end_dt,
            game_duration_seconds=duration_seconds,
        ):
            continue

        participant = find_participant(match, puuid)
        if participant is None:
            print(
                f"warning: player not found in match {match_id}; skipping",
                file=sys.stderr,
            )
            continue

        participant_id = participant.get("participantId")
        team_id = participant.get("teamId")

        summary = MatchSummary(
            matchId=match_id,
            champion=str(participant.get("championName") or "Unknown"),
            win=bool(participant.get("win")),
            kills=int(participant.get("kills") or 0),
            deaths=int(participant.get("deaths") or 0),
            assists=int(participant.get("assists") or 0),
            gameStartTimestamp=game_start_ms,
            gameDurationSeconds=duration_seconds,
            queueId=info.get("queueId"),
            gameCreation=info.get("gameCreation"),
            gameEndTimestamp=info.get("gameEndTimestamp"),
            participantId=int(participant_id) if participant_id is not None else None,
            teamPosition=str(participant.get("teamPosition") or "") or None,
            laneOpponentChampion=find_lane_opponent_champion(match, participant),
        )

        if participant_id is None:
            summary.error = "missing participantId"
            summaries.append(summary)
            continue

        champion_by_id: dict[int, str] = {}
        for p in info.get("participants") or []:
            pid = p.get("participantId")
            cname = p.get("championName")
            if pid is not None and cname:
                champion_by_id[int(pid)] = str(cname)

        try:
            timeline = api.get_timeline(match_id)
            summary.events = extract_player_events(
                timeline,
                participant_id=int(participant_id),
                team_id=int(team_id) if team_id is not None else None,
                champion_by_id=champion_by_id,
            )
            summary.events = add_game_bookend_events(
                summary.events,
                timeline=timeline,
                game_duration_seconds=duration_seconds,
                win=summary.win,
            )
        except RiotAPIError as exc:
            summary.error = f"timeline unavailable: {exc}"
            print(
                f"warning: timeline failed for {match_id}: {exc}",
                file=sys.stderr,
            )

        if video_start is not None and summary.events:
            apply_video_offsets(
                summary.events,
                game_start_ms=game_start_ms,
                video_start=video_start,
                kind=video_kind,
                pre_roll=pre_roll,
                post_roll=post_roll,
            )

        summaries.append(summary)

    summaries.sort(key=lambda m: m.gameStartTimestamp)
    result.matches = summaries
    return result


def print_summary(result: IndexResult) -> None:
    print(result.riot_id)
    if result.vod:
        title = result.vod.get("title") or result.vod.get("datasetId")
        print(f"VOD: {title}")
        print(f"URL: {result.vod.get('url')}")
    print()

    if not result.matches:
        print("No matches found.")
        return

    for match in result.matches:
        outcome = "WIN" if match.win else "LOSS"
        print(match.matchId)
        print(
            f"{match.champion} — "
            f"{match.kills}/{match.deaths}/{match.assists} — {outcome}"
        )
        if match.error:
            print(f"  (warning: {match.error})")
        print()
        if not match.events:
            print("(no player events)")
            print()
            continue
        for event in match.events:
            line = f"{event.gameTime}  {event.type}"
            marker = None
            label = None
            if event.vodTime is not None:
                marker = event.vodTime
                label = "VOD"
            elif event.obsTime is not None:
                marker = event.obsTime
                label = "OBS"
            if marker is not None:
                line += f"  ({label} {marker}"
                if event.clipStart and event.clipEnd:
                    line += f", clip {event.clipStart}-{event.clipEnd}"
                line += ")"
            print(line)
        print()


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)

    api_key = os.environ.get("RIOT_API_KEY")
    if not api_key:
        print(
            "error: RIOT_API_KEY environment variable is required",
            file=sys.stderr,
        )
        return 1

    riot_id = args.riot_id or os.environ.get("RIOT_ID")
    if not riot_id:
        print(
            "error: provide --riot-id or set RIOT_ID "
            '(e.g. export RIOT_ID="lolAmbrosek#twtv")',
            file=sys.stderr,
        )
        return 1

    region = args.region or os.environ.get("RIOT_REGION") or "americas"

    try:
        game_name, tag_line = parse_riot_id(riot_id)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.vod_dir and args.obs_start:
        print("error: use either --vod-dir or --obs-start, not both", file=sys.stderr)
        return 1

    start_dt = None
    end_dt = None
    video_start = None
    video_kind = "obs"
    vod_info: dict[str, Any] | None = None

    try:
        if args.vod_dir:
            vod_info = load_vod_timeline(args.vod_dir)
            video_start = vod_info["start"]
            video_kind = "vod"
            # Default the match window to the VOD span unless overridden.
            start_dt = video_start
            end_dt = vod_info.get("end")

        if args.start_time:
            start_dt = parse_iso_datetime(args.start_time)
        if args.end_time:
            end_dt = parse_iso_datetime(args.end_time)
        if args.obs_start:
            video_start = parse_iso_datetime(args.obs_start)
            video_kind = "obs"
    except (ValueError, FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.count < 1:
        print("error: --count must be at least 1", file=sys.stderr)
        return 1

    api = RiotAPI(api_key=api_key, region=region)

    try:
        account = api.get_account_by_riot_id(game_name, tag_line)
    except RiotAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except RiotNotFoundError:
        print(
            f'error: Riot ID not found: "{game_name}#{tag_line}"',
            file=sys.stderr,
        )
        return 1
    except RiotAPIError as exc:
        print(f"error: failed to resolve Riot ID: {exc}", file=sys.stderr)
        return 1

    puuid = account.get("puuid")
    if not puuid:
        print("error: account response missing puuid", file=sys.stderr)
        return 1

    # Prefer canonical casing from the API when available.
    resolved_id = (
        f"{account.get('gameName', game_name)}#{account.get('tagLine', tag_line)}"
    )

    start_epoch = to_epoch_seconds(start_dt) if start_dt else None
    end_epoch = to_epoch_seconds(end_dt) if end_dt else None
    # Pad match-ID query so games already in progress at VOD start are listed.
    id_start_epoch = start_epoch
    if args.vod_dir and start_epoch is not None:
        id_start_epoch = start_epoch - 45 * 60
    # For a VOD window, fetch enough match IDs to cover a long stream.
    fetch_count = min(max(args.count, 1), 100)
    if args.vod_dir:
        fetch_count = 100

    try:
        match_ids = api.get_match_ids(
            puuid,
            count=fetch_count,
            start=0,
            start_time=id_start_epoch,
            end_time=end_epoch,
        )
    except RiotAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except RiotAPIError as exc:
        print(f"error: failed to fetch match IDs: {exc}", file=sys.stderr)
        return 1

    # Cap to requested count before detail fetches (time filter may drop some).
    if start_epoch is None and end_epoch is None:
        match_ids = match_ids[: args.count]

    result = index_matches(
        api=api,
        riot_id=resolved_id,
        puuid=puuid,
        match_ids=match_ids,
        start_dt=start_dt,
        end_dt=end_dt,
        video_start=video_start,
        video_kind=video_kind,
        pre_roll=args.pre_roll,
        post_roll=args.post_roll,
    )

    if vod_info is not None:
        result.vod = {
            "datasetId": vod_info["datasetId"],
            "title": vod_info.get("title"),
            "uploader": vod_info.get("uploader"),
            "url": vod_info.get("url"),
            "startTimestamp": int(vod_info["start"].timestamp()),
            "durationSeconds": vod_info.get("durationSeconds"),
            "dir": vod_info.get("dir"),
        }

    # If a time window was used, keep at most --count matches after filtering
    # (unless --vod-dir, where we keep every match that falls in the VOD).
    if (start_dt is not None or end_dt is not None) and not args.vod_dir:
        result.matches = result.matches[: args.count]

    print_summary(result)

    try:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(result.to_dict(), fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    except OSError as exc:
        print(f"error: failed to write {args.output}: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
