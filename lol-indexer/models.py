"""Data helpers for LoL match event indexing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def format_mmss(ms: int) -> str:
    """Format milliseconds as mm:ss (or h:mm:ss if >= 1 hour)."""
    total_seconds = max(0, ms) // 1000
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def format_hms(seconds: float) -> str:
    """Format seconds as HH:MM:SS (or H:MM:SS)."""
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def parse_iso_datetime(value: str) -> datetime:
    """Parse an ISO-8601 datetime, accepting a trailing Z."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"Datetime must include a timezone offset: {value}")
    return dt


def to_epoch_seconds(dt: datetime) -> int:
    return int(dt.timestamp())


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_riot_id(riot_id: str) -> tuple[str, str]:
    """Split GameName#TAG into (gameName, tagLine)."""
    if "#" not in riot_id:
        raise ValueError('Riot ID must be in the form "GameName#TAG"')
    game_name, tag_line = riot_id.rsplit("#", 1)
    game_name = game_name.strip()
    tag_line = tag_line.strip()
    if not game_name or not tag_line:
        raise ValueError('Riot ID must be in the form "GameName#TAG"')
    return game_name, tag_line


@dataclass
class PlayerEvent:
    type: str
    gameTimeMs: int
    gameTime: str
    obsOffsetSeconds: float | None = None
    obsTime: str | None = None
    vodOffsetSeconds: float | None = None
    vodTime: str | None = None
    clipStart: str | None = None
    clipEnd: str | None = None
    opponentChampion: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "type": self.type,
            "gameTimeMs": self.gameTimeMs,
            "gameTime": self.gameTime,
        }
        if self.obsOffsetSeconds is not None:
            data["obsOffsetSeconds"] = self.obsOffsetSeconds
        if self.obsTime is not None:
            data["obsTime"] = self.obsTime
        if self.vodOffsetSeconds is not None:
            data["vodOffsetSeconds"] = self.vodOffsetSeconds
        if self.vodTime is not None:
            data["vodTime"] = self.vodTime
        if self.clipStart is not None:
            data["clipStart"] = self.clipStart
        if self.clipEnd is not None:
            data["clipEnd"] = self.clipEnd
        if self.opponentChampion is not None:
            data["opponentChampion"] = self.opponentChampion
        return data


@dataclass
class MatchSummary:
    matchId: str
    champion: str
    win: bool
    kills: int
    deaths: int
    assists: int
    gameStartTimestamp: int
    gameDurationSeconds: int
    queueId: int | None = None
    gameCreation: int | None = None
    gameEndTimestamp: int | None = None
    participantId: int | None = None
    teamPosition: str | None = None
    events: list[PlayerEvent] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "matchId": self.matchId,
            "champion": self.champion,
            "win": self.win,
            "kills": self.kills,
            "deaths": self.deaths,
            "assists": self.assists,
            "gameStartTimestamp": self.gameStartTimestamp,
            "gameDurationSeconds": self.gameDurationSeconds,
            "events": [e.to_dict() for e in self.events],
        }
        if self.queueId is not None:
            data["queueId"] = self.queueId
        if self.gameCreation is not None:
            data["gameCreation"] = self.gameCreation
        if self.gameEndTimestamp is not None:
            data["gameEndTimestamp"] = self.gameEndTimestamp
        if self.participantId is not None:
            data["participantId"] = self.participantId
        if self.teamPosition is not None:
            data["teamPosition"] = self.teamPosition
        if self.error is not None:
            data["error"] = self.error
        return data


@dataclass
class IndexResult:
    riot_id: str
    puuid: str
    matches: list[MatchSummary] = field(default_factory=list)
    generated_at: str = field(default_factory=utc_now_iso)
    vod: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "player": {
                "riotId": self.riot_id,
                "puuid": self.puuid,
            },
            "generatedAt": self.generated_at,
            "matches": [m.to_dict() for m in self.matches],
        }
        if self.vod is not None:
            data["vod"] = self.vod
        return data


MONSTER_TYPE_MAP = {
    "DRAGON": "DRAGON",
    "BARON_NASHOR": "BARON",
    "RIFTHERALD": "HERALD",
    "HORDE": "HORDE",
}

BUILDING_TYPE_MAP = {
    "TOWER_BUILDING": "TOWER",
    "INHIBITOR_BUILDING": "INHIBITOR",
}


def extract_player_events(
    timeline: dict[str, Any],
    participant_id: int,
    team_id: int | None = None,
    *,
    champion_by_id: dict[int, str] | None = None,
) -> list[PlayerEvent]:
    """Walk timeline frames and collect KILL/DEATH/ASSIST (+ optional objectives)."""
    events: list[PlayerEvent] = []
    frames = (timeline.get("info") or {}).get("frames") or []
    champs = champion_by_id or {}

    def opponent_name(other_id: Any) -> str | None:
        if other_id is None:
            return None
        try:
            name = champs.get(int(other_id))
        except (TypeError, ValueError):
            return None
        return str(name) if name else None

    for frame in frames:
        for event in frame.get("events") or []:
            event_type = event.get("type")
            timestamp = event.get("timestamp")
            if timestamp is None:
                continue

            if event_type == "CHAMPION_KILL":
                killer_id = event.get("killerId")
                victim_id = event.get("victimId")
                assisting = event.get("assistingParticipantIds") or []

                if killer_id == participant_id:
                    events.append(
                        PlayerEvent(
                            type="KILL",
                            gameTimeMs=int(timestamp),
                            gameTime=format_mmss(int(timestamp)),
                            opponentChampion=opponent_name(victim_id),
                        )
                    )
                elif victim_id == participant_id:
                    events.append(
                        PlayerEvent(
                            type="DEATH",
                            gameTimeMs=int(timestamp),
                            gameTime=format_mmss(int(timestamp)),
                            opponentChampion=opponent_name(killer_id),
                        )
                    )
                elif participant_id in assisting:
                    # Do not count as ASSIST if already counted as KILL.
                    events.append(
                        PlayerEvent(
                            type="ASSIST",
                            gameTimeMs=int(timestamp),
                            gameTime=format_mmss(int(timestamp)),
                            opponentChampion=opponent_name(victim_id),
                        )
                    )

            elif event_type == "ELITE_MONSTER_KILL" and team_id is not None:
                # killerTeamId is the reliable team filter when present.
                killer_team = event.get("killerTeamId")
                killer_id = event.get("killerId")
                assisting = event.get("assistingParticipantIds") or []
                team_responsible = False
                if killer_team is not None:
                    team_responsible = killer_team == team_id
                elif killer_id == participant_id or participant_id in assisting:
                    team_responsible = True

                if not team_responsible:
                    continue

                monster = event.get("monsterType")
                mapped = MONSTER_TYPE_MAP.get(monster)
                if mapped:
                    events.append(
                        PlayerEvent(
                            type=mapped,
                            gameTimeMs=int(timestamp),
                            gameTime=format_mmss(int(timestamp)),
                        )
                    )

            elif event_type == "BUILDING_KILL" and team_id is not None:
                # Buildings are destroyed by the opposing team; teamId on the
                # event is the team that owned the building.
                building_team = event.get("teamId")
                if building_team is None or building_team == team_id:
                    continue
                building = event.get("buildingType")
                mapped = BUILDING_TYPE_MAP.get(building)
                if mapped:
                    events.append(
                        PlayerEvent(
                            type=mapped,
                            gameTimeMs=int(timestamp),
                            gameTime=format_mmss(int(timestamp)),
                        )
                    )

    events.sort(key=lambda e: e.gameTimeMs)
    return events


def add_game_bookend_events(
    events: list[PlayerEvent],
    *,
    timeline: dict[str, Any],
    game_duration_seconds: int,
    win: bool,
) -> list[PlayerEvent]:
    """
    Ensure GAME_START + GAME_END bookends exist.

    GAME_END prefers timeline GAME_END (nexus / victory screen), else duration.
    """
    out = list(events)
    has_start = any(e.type == "GAME_START" for e in out)
    has_end = any(e.type == "GAME_END" for e in out)

    if not has_start:
        out.append(
            PlayerEvent(
                type="GAME_START",
                gameTimeMs=0,
                gameTime=format_mmss(0),
            )
        )

    if not has_end:
        end_ms = max(0, int(game_duration_seconds) * 1000)
        frames = (timeline.get("info") or {}).get("frames") or []
        for frame in frames:
            for event in frame.get("events") or []:
                if event.get("type") == "GAME_END" and event.get("timestamp") is not None:
                    end_ms = int(event["timestamp"])
                    break
        out.append(
            PlayerEvent(
                type="GAME_END",
                gameTimeMs=end_ms,
                gameTime=format_mmss(end_ms),
                opponentChampion="WIN" if win else "LOSS",
            )
        )

    out.sort(key=lambda e: e.gameTimeMs)
    return out


def apply_video_offsets(
    events: list[PlayerEvent],
    game_start_ms: int,
    video_start: datetime,
    *,
    kind: str = "obs",
    pre_roll: int = 15,
    post_roll: int = 10,
) -> None:
    """Annotate events with video offsets and clip ranges in-place.

    kind:
      - "obs": sets obsOffsetSeconds / obsTime
      - "vod": sets vodOffsetSeconds / vodTime
    """
    video_start_ms = int(video_start.timestamp() * 1000)

    for event in events:
        absolute_ms = game_start_ms + event.gameTimeMs
        offset_seconds = (absolute_ms - video_start_ms) / 1000.0
        rounded = round(offset_seconds, 1)
        clock = format_hms(max(0.0, offset_seconds))

        if kind == "vod":
            event.vodOffsetSeconds = rounded
            event.vodTime = clock
        else:
            event.obsOffsetSeconds = rounded
            event.obsTime = clock

        clip_start_s = max(0.0, offset_seconds - pre_roll)
        clip_end_s = max(0.0, offset_seconds + post_roll)
        event.clipStart = format_hms(clip_start_s)
        event.clipEnd = format_hms(clip_end_s)


def apply_obs_offsets(
    events: list[PlayerEvent],
    game_start_ms: int,
    obs_start: datetime,
    pre_roll: int = 15,
    post_roll: int = 10,
) -> None:
    """Backward-compatible wrapper for OBS recordings."""
    apply_video_offsets(
        events,
        game_start_ms=game_start_ms,
        video_start=obs_start,
        kind="obs",
        pre_roll=pre_roll,
        post_roll=post_roll,
    )
