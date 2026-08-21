"""YouTube Data API upload. Uploads land as private so you finish them in Studio.

Auth is the OAuth installed-app flow: one browser consent, then a refresh token
cached under .secrets/. Each upload costs 1600 of the default 10,000 daily quota
units, so roughly six uploads a day unless the project has an increase.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from posting.creds import find_client_secrets, secret_dir

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CHUNK_SIZE = 4 * 1024 * 1024
GAMING_CATEGORY_ID = "20"
PRIVACY_LEVELS = ("private", "unlisted", "public")

PIP_HINT = "pip install -r requirements-post.txt"


def token_path() -> Path:
    override = (os.environ.get("YOUTUBE_TOKEN_FILE") or "").strip()
    return Path(override).expanduser() if override else secret_dir() / "youtube_token.json"


def _client_secrets_file() -> Path | None:
    raw = (os.environ.get("YOUTUBE_CLIENT_SECRETS") or "").strip()
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_file() else None
    return find_client_secrets()


def _client_config() -> dict[str, Any] | None:
    client_id = (os.environ.get("YOUTUBE_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("YOUTUBE_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        return None
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def _api_key_only() -> bool:
    """An AIza… key is read-only; videos.insert needs OAuth on behalf of the channel."""
    for name in ("YOUTUBE_KEY", "YOUTUBE_API_KEY"):
        if (os.environ.get(name) or "").strip().startswith("AIza"):
            return True
    return False


SETUP_HINT = (
    "YouTube uploads need an OAuth client, not an API key. In the Google Cloud console: "
    "enable YouTube Data API v3, then Credentials → Create credentials → OAuth client ID → "
    "Desktop app. Drop the downloaded json into secrets/ and it is picked up automatically."
)


def configured() -> bool:
    return bool(_client_secrets_file() or _client_config())


def authorized() -> bool:
    return token_path().is_file()


def status() -> dict[str, Any]:
    ready = configured()
    return {
        "platform": "youtube",
        "configured": ready,
        "authorized": authorized(),
        "tokenFile": str(token_path()),
        "hint": "" if ready else SETUP_HINT,
        "apiKeyOnly": not ready and _api_key_only(),
    }


def _deps() -> dict[str, Any]:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise RuntimeError(f"YouTube upload needs the Google API client ({PIP_HINT})") from exc
    return {
        "Request": Request,
        "Credentials": Credentials,
        "InstalledAppFlow": InstalledAppFlow,
        "build": build,
        "HttpError": HttpError,
        "MediaFileUpload": MediaFileUpload,
    }


def _save(creds: Any) -> None:
    dest = token_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(creds.to_json(), encoding="utf-8")
    try:
        dest.chmod(0o600)
    except OSError:
        pass


def login(*, port: int = 0) -> dict[str, Any]:
    """Run the browser consent flow and cache the refresh token."""
    dep = _deps()
    secrets_file = _client_secrets_file()
    if secrets_file is not None:
        flow = dep["InstalledAppFlow"].from_client_secrets_file(str(secrets_file), SCOPES)
    else:
        config = _client_config()
        if config is None:
            prefix = "YOUTUBE_KEY is an API key and cannot upload. " if _api_key_only() else ""
            raise RuntimeError(prefix + SETUP_HINT)
        flow = dep["InstalledAppFlow"].from_client_config(config, SCOPES)
    creds = flow.run_local_server(port=port, prompt="consent", open_browser=True)
    _save(creds)
    return {"ok": True, "tokenFile": str(token_path())}


def credentials() -> Any:
    dep = _deps()
    path = token_path()
    if not path.is_file():
        raise RuntimeError("YouTube is not authorized yet. Run: python post_short.py --login youtube")
    creds = dep["Credentials"].from_authorized_user_file(str(path), SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        creds.refresh(dep["Request"]())
        _save(creds)
        return creds
    raise RuntimeError("YouTube token is stale. Run: python post_short.py --login youtube")


def _service() -> Any:
    dep = _deps()
    return dep["build"]("youtube", "v3", credentials=credentials(), cache_discovery=False)


def upload(
    video: Path,
    *,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    category_id: str = GAMING_CATEGORY_ID,
    privacy: str = "private",
    made_for_kids: bool = False,
    on_progress: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Resumable upload. Returns the new video id and its Studio/watch links."""
    dep = _deps()
    video = video.resolve()
    if not video.is_file():
        raise FileNotFoundError(video)
    if privacy not in PRIVACY_LEVELS:
        raise ValueError(f"privacy must be {', '.join(PRIVACY_LEVELS)}")

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": list(tags or []),
            "categoryId": str(category_id),
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": bool(made_for_kids),
        },
    }
    media = dep["MediaFileUpload"](
        str(video),
        mimetype="video/mp4",
        chunksize=CHUNK_SIZE,
        resumable=True,
    )
    request = _service().videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    try:
        while response is None:
            progress, response = request.next_chunk()
            if progress is not None and on_progress is not None:
                on_progress(int(progress.progress() * 100))
    except dep["HttpError"] as exc:
        raise RuntimeError(_http_error_text(exc)) from exc

    video_id = str((response or {}).get("id") or "")
    if not video_id:
        raise RuntimeError("YouTube did not return a video id")
    return {
        "videoId": video_id,
        "url": f"https://youtu.be/{video_id}",
        "studioUrl": f"https://studio.youtube.com/video/{video_id}/edit",
        "privacy": privacy,
        "madeForKids": bool(made_for_kids),
        "categoryId": str(category_id),
    }


def _http_error_text(exc: Any) -> str:
    reason = ""
    try:
        import json

        payload = json.loads(exc.content.decode("utf-8"))
        errors = (payload.get("error") or {}).get("errors") or []
        reason = str(errors[0].get("reason") or "") if errors else ""
        message = str((payload.get("error") or {}).get("message") or "")
    except Exception:
        message = str(exc)
    if reason in {"quotaExceeded", "uploadLimitExceeded"}:
        return (
            "YouTube upload quota is used up for today "
            "(each upload costs 1600 of 10,000 default daily units)."
        )
    if reason == "youtubeSignupRequired":
        return "That Google account has no YouTube channel. Create one, then re-run --login youtube."
    return f"YouTube API error{f' ({reason})' if reason else ''}: {message}"
