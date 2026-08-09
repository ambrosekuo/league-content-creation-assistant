#!/usr/bin/env python3
"""List recent Twitch VODs (past broadcasts) for a channel via Helix API."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from env_loader import load_dotenv


HELIX = "https://api.twitch.tv/helix"
ID_TWITCH = "https://id.twitch.tv"


def http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 20.0,
) -> Any:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
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


def get_app_access_token(client_id: str, client_secret: str) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        }
    )
    url = f"{ID_TWITCH}/oauth2/token?{query}"
    payload = http_json(url, method="POST")
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("Twitch token response missing access_token")
    return str(token)


def get_user_id(client_id: str, token: str, login: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({"login": login.lower()})
    url = f"{HELIX}/users?{query}"
    payload = http_json(
        url,
        headers={
            "Client-ID": client_id,
            "Authorization": f"Bearer {token}",
        },
    )
    users = payload.get("data") or []
    if not users:
        raise RuntimeError(f'Twitch user not found: "{login}"')
    return users[0]


def list_archives(
    client_id: str,
    token: str,
    user_id: str,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    videos: list[dict[str, Any]] = []
    cursor: str | None = None
    remaining = max(1, limit)

    while remaining > 0:
        page_size = min(100, remaining)
        params: dict[str, Any] = {
            "user_id": user_id,
            "type": "archive",
            "first": page_size,
            "sort": "time",
        }
        if cursor:
            params["after"] = cursor
        query = urllib.parse.urlencode(params)
        payload = http_json(
            f"{HELIX}/videos?{query}",
            headers={
                "Client-ID": client_id,
                "Authorization": f"Bearer {token}",
            },
        )
        batch = payload.get("data") or []
        if not batch:
            break
        videos.extend(batch)
        remaining -= len(batch)
        cursor = (payload.get("pagination") or {}).get("cursor")
        if not cursor:
            break

    return videos[:limit]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="List recent past-broadcast VODs for a Twitch channel."
    )
    parser.add_argument(
        "--channel",
        default=None,
        help="Twitch login (default: TWITCH_CHANNEL env)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max VODs to return (default: 20)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print raw JSON instead of a table",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)

    client_id = os.environ.get("TWITCH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
    channel = (args.channel or os.environ.get("TWITCH_CHANNEL") or "").strip()

    if not client_id or not client_secret:
        print(
            "error: set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in .env",
            file=sys.stderr,
        )
        return 1
    if not channel:
        print(
            "error: provide --channel or set TWITCH_CHANNEL in .env",
            file=sys.stderr,
        )
        return 1
    if args.limit < 1:
        print("error: --limit must be at least 1", file=sys.stderr)
        return 1

    try:
        token = get_app_access_token(client_id, client_secret)
        user = get_user_id(client_id, token, channel)
        videos = list_archives(
            client_id,
            token,
            str(user["id"]),
            limit=args.limit,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"channel": channel, "user": user, "videos": videos}, indent=2))
        return 0

    if not videos:
        print(f"No archive VODs found for {channel}.")
        return 0

    print(f"{user.get('display_name') or channel} — {len(videos)} VOD(s)\n")
    for vod in videos:
        print(vod.get("id"))
        print(vod.get("title") or "(untitled)")
        print(
            f"{vod.get('created_at')}  ·  {vod.get('duration')}  ·  "
            f"{vod.get('view_count')} views"
        )
        print(vod.get("url"))
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
