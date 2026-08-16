"""Minimal Riot Games API client (stdlib HTTP)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class RiotAPIError(Exception):
    """Base error for Riot API failures."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RiotAuthError(RiotAPIError):
    """401 / 403 — invalid, expired, or forbidden API key."""


class RiotNotFoundError(RiotAPIError):
    """404 — Riot ID or match not found."""


class RiotRateLimitError(RiotAPIError):
    """429 — rate limited (raised after retries exhausted)."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message, status=429)
        self.retry_after = retry_after


class RiotServerError(RiotAPIError):
    """5xx — Riot API server problem."""


class RiotAPI:
    """Thin wrapper around Riot regional routing endpoints."""

    def __init__(
        self,
        api_key: str,
        region: str = "americas",
        timeout: float = 15.0,
        max_retries: int = 3,
    ) -> None:
        if not api_key or not api_key.strip():
            raise RiotAuthError("RIOT_API_KEY is missing or empty", status=None)
        self._api_key = api_key.strip()
        self.region = region.strip().lower()
        self.timeout = timeout
        self.max_retries = max_retries
        self.base_url = f"https://{self.region}.api.riotgames.com"

    def _request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = ""
        if params:
            filtered = {k: v for k, v in params.items() if v is not None}
            if filtered:
                query = "?" + urllib.parse.urlencode(filtered)
        url = f"{self.base_url}{path}{query}"

        headers = {
            "X-Riot-Token": self._api_key,
            "Accept": "application/json",
            # Cloudflare (in front of Riot) rejects Python-urllib's default UA (error 1010).
            "User-Agent": "lol-vod-indexer/1.0 (personal; +https://github.com/local)",
        }

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read().decode("utf-8")
                    if not body:
                        return None
                    return json.loads(body)
            except urllib.error.HTTPError as exc:
                retry_after = _parse_retry_after(exc.headers.get("Retry-After"))
                status = exc.code
                # Drain body so connections can close cleanly; never log keys.
                body_text = ""
                try:
                    body_text = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                riot_message = _extract_riot_message(body_text)

                if status in (401, 403):
                    detail = riot_message or (
                        "invalid, expired, or forbidden API key"
                    )
                    raise RiotAuthError(
                        f"Riot API auth failed (HTTP {status}): {detail}",
                        status=status,
                    ) from None
                if status == 404:
                    raise RiotNotFoundError(
                        f"Resource not found (HTTP 404): {path}",
                        status=404,
                    ) from None
                if status == 429:
                    if attempt < self.max_retries:
                        wait = retry_after if retry_after is not None else (1.5 * (attempt + 1))
                        time.sleep(wait)
                        last_error = RiotRateLimitError(
                            f"Rate limited (HTTP 429); retried after {wait:.1f}s",
                            retry_after=retry_after,
                        )
                        continue
                    raise RiotRateLimitError(
                        "Rate limited by Riot API (HTTP 429); retries exhausted",
                        retry_after=retry_after,
                    ) from None
                if 500 <= status <= 599:
                    if attempt < self.max_retries:
                        wait = 1.0 * (attempt + 1)
                        time.sleep(wait)
                        last_error = RiotServerError(
                            f"Riot API server error (HTTP {status})",
                            status=status,
                        )
                        continue
                    raise RiotServerError(
                        f"Riot API server error (HTTP {status})",
                        status=status,
                    ) from None
                raise RiotAPIError(f"Riot API error (HTTP {status})", status=status) from None
            except urllib.error.URLError as exc:
                if attempt < self.max_retries:
                    wait = 1.0 * (attempt + 1)
                    time.sleep(wait)
                    last_error = RiotAPIError(f"Network error: {exc.reason}")
                    continue
                raise RiotAPIError(f"Network error: {exc.reason}") from None

        if last_error:
            raise last_error
        raise RiotAPIError("Request failed with no response")

    def get_account_by_riot_id(self, game_name: str, tag_line: str) -> dict[str, Any]:
        encoded_name = urllib.parse.quote(game_name, safe="")
        encoded_tag = urllib.parse.quote(tag_line, safe="")
        path = f"/riot/account/v1/accounts/by-riot-id/{encoded_name}/{encoded_tag}"
        return self._request(path)

    def get_match_ids(
        self,
        puuid: str,
        count: int = 10,
        start: int = 0,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[str]:
        encoded = urllib.parse.quote(puuid, safe="")
        path = f"/lol/match/v5/matches/by-puuid/{encoded}/ids"
        params: dict[str, Any] = {
            "start": start,
            "count": min(max(count, 0), 100),
        }
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        result = self._request(path, params=params)
        return list(result or [])

    def get_match(self, match_id: str) -> dict[str, Any]:
        encoded = urllib.parse.quote(match_id, safe="")
        return self._request(f"/lol/match/v5/matches/{encoded}")

    def get_timeline(self, match_id: str) -> dict[str, Any]:
        encoded = urllib.parse.quote(match_id, safe="")
        return self._request(f"/lol/match/v5/matches/{encoded}/timeline")

    def get_league_entries_by_puuid(
        self,
        puuid: str,
        *,
        platform: str = "na1",
    ) -> list[dict[str, Any]]:
        """Platform routing (na1/euw1/…). Returns ranked queue entries."""
        encoded = urllib.parse.quote(puuid, safe="")
        path = f"/lol/league/v4/entries/by-puuid/{encoded}"
        # Temporarily hit platform host instead of regional routing.
        original = self.base_url
        self.base_url = f"https://{platform.strip().lower()}.api.riotgames.com"
        try:
            result = self._request(path)
        finally:
            self.base_url = original
        return list(result or [])

    def get_challenger_league(
        self,
        *,
        queue: str = "RANKED_SOLO_5x5",
        platform: str = "na1",
    ) -> dict[str, Any]:
        """Platform routing. Full Challenger ladder for a queue."""
        path = f"/lol/league/v4/challengerleagues/by-queue/{queue}"
        original = self.base_url
        self.base_url = f"https://{platform.strip().lower()}.api.riotgames.com"
        try:
            result = self._request(path)
        finally:
            self.base_url = original
        return dict(result or {})

    def get_grandmaster_league(
        self,
        *,
        queue: str = "RANKED_SOLO_5x5",
        platform: str = "na1",
    ) -> dict[str, Any]:
        path = f"/lol/league/v4/grandmasterleagues/by-queue/{queue}"
        original = self.base_url
        self.base_url = f"https://{platform.strip().lower()}.api.riotgames.com"
        try:
            result = self._request(path)
        finally:
            self.base_url = original
        return dict(result or {})

    def get_league_exp_page(
        self,
        *,
        queue: str = "RANKED_SOLO_5x5",
        tier: str = "CHALLENGER",
        division: str = "I",
        page: int = 1,
        platform: str = "na1",
    ) -> list[dict[str, Any]]:
        path = f"/lol/league-exp/v4/entries/{queue}/{tier}/{division}"
        original = self.base_url
        self.base_url = f"https://{platform.strip().lower()}.api.riotgames.com"
        try:
            result = self._request(path, {"page": page})
        finally:
            self.base_url = original
        return list(result or [])


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _extract_riot_message(body_text: str) -> str | None:
    if not body_text:
        return None
    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError:
        return None
    status = payload.get("status")
    if isinstance(status, dict) and status.get("message"):
        return str(status["message"])
    if payload.get("detail"):
        return str(payload["detail"])
    return None
