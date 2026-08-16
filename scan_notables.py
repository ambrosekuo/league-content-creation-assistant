#!/usr/bin/env python3
"""Scan recent games for overlay notables (pros + Twitch partners).

Default input is the U.GG dump at lp_data.txt (last 20 ranked games).

  python scan_notables.py
  python scan_notables.py --input lp_data.txt --limit 20
  python scan_notables.py --no-twitch   # pros.json only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from env_loader import load_dotenv
from generate_lobby_card import cached_bytes, ddragon_version
from render_rank_cards import detect_streamers

ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "lp_data.txt"

# U.GG MatchSummary.role integers
UGG_ROLES = {
    1: "JUNGLE",
    2: "UTILITY",
    3: "BOTTOM",
    4: "MIDDLE",
    5: "TOP",
}


def load_champ_names() -> dict[int, str]:
    ver = ddragon_version()
    raw = cached_bytes(f"https://ddragon.leagueoflegends.com/cdn/{ver}/data/en_US/champion.json")
    if not raw:
        return {}
    data = json.loads(raw.decode("utf-8"))
    out: dict[int, str] = {}
    for row in (data.get("data") or {}).values():
        try:
            out[int(row["key"])] = str(row.get("name") or row.get("id") or "")
        except (TypeError, ValueError, KeyError):
            continue
    return out


def champ_name(champ_id: Any, names: dict[int, str]) -> str:
    try:
        cid = int(champ_id)
    except (TypeError, ValueError):
        return "?"
    return names.get(cid) or f"#{cid}"


def extract_matches(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [m for m in payload if isinstance(m, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") or payload
    summaries = data.get("fetchPlayerMatchSummaries") or data
    matches = summaries.get("matchSummaries") or payload.get("matchSummaries")
    if isinstance(matches, list):
        return [m for m in matches if isinstance(m, dict)]
    return []


def is_me(player: dict[str, Any], me_names: set[str]) -> bool:
    name = str(player.get("riotUserName") or player.get("name") or "").strip().lower()
    tag = str(player.get("riotTagLine") or player.get("tag") or "").strip().lower()
    if name in me_names or f"{name}#{tag}" in me_names:
        return True
    return any(token and token in name for token in me_names if len(token) >= 4)


def me_tokens() -> set[str]:
    tokens = {"lolambrosek", "twtv lolambrosek", "lolambrosek#twtv"}
    raw = (os.environ.get("RIOT_ID") or "").strip()
    if raw:
        tokens.add(raw.lower())
        if "#" in raw:
            tokens.add(raw.split("#", 1)[0].strip().lower())
    return tokens


def team_players(match: dict[str, Any]) -> list[dict[str, Any]]:
    players: list[dict[str, Any]] = []
    for key in ("teamA", "teamB"):
        for row in match.get(key) or []:
            if isinstance(row, dict):
                players.append(row)
    return players


def match_to_meta(
    match: dict[str, Any],
    *,
    names: dict[int, str],
    me_names: set[str],
) -> dict[str, Any] | None:
    players_raw = team_players(match)
    me_row = next((p for p in players_raw if is_me(p, me_names)), None)
    if me_row is None:
        return None
    me_team = int(me_row.get("teamId") or 0)
    win = bool(match.get("win"))
    players: list[dict[str, Any]] = []
    for row in players_raw:
        team_id = int(row.get("teamId") or 0)
        mine = is_me(row, me_names)
        players.append(
            {
                "teamId": team_id,
                "champion": champ_name(row.get("championId"), names),
                "name": str(row.get("riotUserName") or "").strip(),
                "tag": str(row.get("riotTagLine") or "").strip(),
                "position": UGG_ROLES.get(int(row.get("role") or 0), ""),
                "win": win if team_id == me_team else (not win),
                "mine": mine,
            }
        )
    me = next(p for p in players if p.get("mine"))
    opp = next(
        (
            p
            for p in players
            if not p.get("mine")
            and p.get("teamId") != me_team
            and p.get("position")
            and p.get("position") == me.get("position")
        ),
        None,
    )
    return {
        "me": me,
        "opponent": opp or {},
        "players": players,
        "matchId": match.get("matchId"),
        "win": win,
        "queueType": match.get("queueType"),
        "matchCreationTime": match.get("matchCreationTime"),
        "lp": (match.get("lpInfo") or {}).get("lp"),
    }


def fmt_when(ts: Any) -> str:
    try:
        ms = int(ts)
    except (TypeError, ValueError):
        return ""
    if ms > 10_000_000_000:
        ms //= 1000
    dt = datetime.fromtimestamp(ms, tz=timezone.utc).astimezone()
    return dt.strftime("%b %d")


def kind_label(row: dict[str, Any]) -> str:
    if str(row.get("kind") or "") == "pro":
        org = str(row.get("org") or "").strip()
        return f"PRO{f' {org}' if org else ''}"
    btype = str(row.get("broadcasterType") or "").strip()
    if btype == "partner":
        return "TWITCH PARTNER"
    return str(row.get("source") or "notable").upper()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Find overlay notables in recent games.")
    p.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="U.GG match dump JSON")
    p.add_argument("--limit", type=int, default=20, help="How many recent games (default 20)")
    p.add_argument(
        "--no-twitch",
        action="store_true",
        help="Skip Helix lookup (pros.json only)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv()
    src = args.input.expanduser().resolve()
    if not src.is_file():
        print(f"error: missing {src}", file=sys.stderr)
        return 1
    payload = json.loads(src.read_text(encoding="utf-8"))
    matches = extract_matches(payload)[: max(0, int(args.limit))]
    if not matches:
        print(f"error: no matchSummaries in {src}", file=sys.stderr)
        return 1

    names = load_champ_names()
    me_names = me_tokens()
    lookup = not bool(args.no_twitch)
    print(
        f"Scanning {len(matches)} games from {src.name}  "
        f"({'pros + Twitch partners' if lookup else 'pros.json only'})\n"
    )

    seen: dict[str, dict[str, Any]] = {}
    games_with = 0
    for i, match in enumerate(matches, 1):
        meta = match_to_meta(match, names=names, me_names=me_names)
        if meta is None:
            print(f"#{i:>2}  skip (could not find you in lobby)")
            continue
        notables = detect_streamers(meta, lookup=lookup, quiet=True)
        me = meta["me"]
        opp = meta.get("opponent") or {}
        result = "W" if meta.get("win") else "L"
        lp = meta.get("lp")
        lp_s = f"{int(lp):+d} LP" if isinstance(lp, (int, float)) else ""
        vs = str(opp.get("champion") or "?").strip() or "?"
        when = fmt_when(meta.get("matchCreationTime"))
        print(
            f"#{i:>2}  {result}  {me.get('champion')} {me.get('position') or ''} vs {vs}"
            f"  {when}  {lp_s}".rstrip()
        )
        if not notables:
            print("     —")
            continue
        games_with += 1
        for row in notables:
            side = "ally" if row.get("ally") else "enemy"
            print(
                f"     {row['display']:<18}  {side:<5}  {row.get('champion') or '?':<12}  "
                f"{kind_label(row)}"
            )
            key = str(row.get("id") or row.get("display") or "").lower()
            bucket = seen.setdefault(
                key,
                {
                    "display": row.get("display"),
                    "kind": kind_label(row),
                    "games": 0,
                    "appearances": [],
                },
            )
            bucket["games"] += 1
            bucket["appearances"].append(
                f"{side} {row.get('champion')} (#{i} {result})"
            )

    print("\n" + "—" * 56)
    if not seen:
        print("No pros or Twitch partners in these games.")
        return 0
    print(f"Significant players  ({len(seen)} unique, in {games_with}/{len(matches)} games)\n")
    ranked = sorted(
        seen.values(),
        key=lambda r: (-int(r["games"]), 0 if str(r["kind"]).startswith("PRO") else 1, str(r["display"])),
    )
    for row in ranked:
        print(f"  {row['display']:<18}  {row['kind']:<16}  {row['games']} game(s)")
        for line in row["appearances"]:
            print(f"      {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
