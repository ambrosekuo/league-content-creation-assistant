"""TikTok Content Posting API: upload to the creator's draft inbox.

The video.upload scope sends the file into TikTok without posting it. TikTok then
shows an inbox notification; tapping it opens the normal editor where you add the
caption, sounds and cover, then post. TikTok caps this at 5 pending uploads per
24 hours and 6 init calls per minute.

Auth is TikTok's desktop flow, which differs from plain OAuth in three ways worth
knowing before touching this file:

  * PKCE is mandatory, and code_challenge is the *hex* of SHA256(verifier),
    not the base64url the RFC asks for.
  * The token exchange still wants client_secret alongside code_verifier.
  * The redirect URI has to be loopback (127.0.0.1 / localhost), may use plain
    http, and must carry a port. A "*" port is allowed in the portal.

Scope access follows app review: the plumbing here works as soon as the client
key exists, but TikTok only honours video.upload once the app is approved.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import math
import os
import secrets
import socket
import string
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Callable

from posting.creds import find_secret_file, read_json_secret, secret_dir

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
INBOX_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"

SCOPE = "video.upload"
CLIENT_FILE = "tiktok_client.json"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8720/callback/"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

# TikTok wants 43-128 unreserved characters; 64 keeps it comfortably inside.
VERIFIER_CHARS = string.ascii_letters + string.digits + "-._~"
VERIFIER_LEN = 64

MB = 1024 * 1024
MIN_CHUNK = 5 * MB
MAX_CHUNK = 64 * MB
DEFAULT_CHUNK = 32 * MB
MAX_CHUNKS = 1000

# Terminal states from /v2/post/publish/status/fetch/.
DONE_STATES = {"SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"}
FAILED_STATES = {"FAILED"}

SETUP_HINT = (
    "TikTok uploads need a developer app: developers.tiktok.com → your app → add the "
    "Content Posting API product with the video.upload scope, set the app type to Desktop "
    f"and register redirect {DEFAULT_REDIRECT_URI} (or http://127.0.0.1:*/callback/). Put the "
    f"client key and secret in secrets/{CLIENT_FILE} or .env, then run: python upload_tiktok.py --login"
)

REVIEW_HINT = (
    "video.upload is granted by TikTok's app review. Until the app flips from In review to "
    "Approved, the login can succeed while uploads are still refused."
)


def token_path() -> Path:
    override = (os.environ.get("TIKTOK_TOKEN_FILE") or "").strip()
    return Path(override).expanduser() if override else secret_dir() / "tiktok_token.json"


def client_file() -> Path | None:
    return find_secret_file(CLIENT_FILE)


def _client_config() -> dict[str, Any]:
    return read_json_secret(CLIENT_FILE)


def _setting(env_name: str, *json_keys: str, default: str = "") -> str:
    """.env wins, then secrets/tiktok_client.json, then the built-in default."""
    value = (os.environ.get(env_name) or "").strip()
    if value:
        return value
    config = _client_config()
    for key in json_keys:
        found = str(config.get(key) or "").strip()
        if found:
            return found
    return default


def _client_key() -> str:
    return _setting("TIKTOK_CLIENT_KEY", "client_key", "clientKey")


def _client_secret() -> str:
    return _setting("TIKTOK_CLIENT_SECRET", "client_secret", "clientSecret")


def _redirect_uri() -> str:
    return _setting(
        "TIKTOK_REDIRECT_URI",
        "redirect_uri",
        "redirectUri",
        default=DEFAULT_REDIRECT_URI,
    )


def configured() -> bool:
    return bool(_client_key() and _client_secret())


def authorized() -> bool:
    return bool(_read_token().get("access_token"))


def granted_scopes() -> list[str]:
    raw = str(_read_token().get("scope") or "")
    return [s.strip() for s in raw.replace(" ", ",").split(",") if s.strip()]


def upload_scope_granted() -> bool:
    return SCOPE in granted_scopes()


def is_loopback(redirect_uri: str) -> bool:
    host = (urllib.parse.urlparse(redirect_uri).hostname or "").lower()
    return host in LOOPBACK_HOSTS


def status() -> dict[str, Any]:
    ready = configured()
    record = _read_token()
    expires_at = int(record.get("expires_at") or 0)
    hint = ""
    if not ready:
        hint = SETUP_HINT
    elif not record.get("access_token"):
        hint = "Run: python upload_tiktok.py --login"
    elif not upload_scope_granted():
        hint = REVIEW_HINT
    return {
        "platform": "tiktok",
        "configured": ready,
        "authorized": bool(record.get("access_token")),
        "uploadScope": upload_scope_granted(),
        "scopes": granted_scopes(),
        "tokenFile": str(token_path()),
        "clientFile": str(client_file() or ""),
        "redirectUri": _redirect_uri() if ready else "",
        "loopback": is_loopback(_redirect_uri()),
        "expiresAt": expires_at,
        "expiresIn": max(0, expires_at - int(time.time())) if expires_at else 0,
        "hint": hint,
    }


def preflight() -> dict[str, Any]:
    """Everything we can check without spending an upload against the daily cap."""
    blockers: list[str] = []
    if not configured():
        blockers.append(SETUP_HINT)
    elif not authorized():
        blockers.append("Not authorized yet. Run: python upload_tiktok.py --login")
    elif not upload_scope_granted():
        blockers.append(
            f"Token scopes are {granted_scopes() or ['none']}, missing {SCOPE}. {REVIEW_HINT}"
        )
    return {"ok": not blockers, "blockers": blockers, **status()}


def _read_token() -> dict[str, Any]:
    path = token_path()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_token(payload: dict[str, Any]) -> None:
    dest = token_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        dest.chmod(0o600)
    except OSError:
        pass


def _post_form(url: str, fields: dict[str, str]) -> dict[str, Any]:
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Cache-Control": "no-cache",
        },
    )
    return _send_json(req)


def _post_json(url: str, payload: dict[str, Any], *, token: str) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
    )
    return _send_json(req)


def _send_json(req: urllib.request.Request) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(req, timeout=120) as res:
            raw = res.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(_error_text(exc.code, detail)) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"TikTok request failed: {exc.reason}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"TikTok returned non-JSON: {raw[:200]}") from exc
    error = payload.get("error") or {}
    code = str(error.get("code") or "").lower()
    if code and code != "ok":
        raise RuntimeError(_error_text(200, raw, code=code, message=str(error.get("message") or "")))
    return payload


def _error_text(http_status: int, raw: str, *, code: str = "", message: str = "") -> str:
    if not code:
        try:
            payload = json.loads(raw)
            error = payload.get("error") or {}
            code = str(error.get("code") or error.get("error") or "")
            message = str(error.get("message") or error.get("error_description") or "")
        except (json.JSONDecodeError, AttributeError):
            message = raw[:300]
    hints = {
        "spam_risk_too_many_pending_share": (
            "TikTok allows at most 5 uploads waiting in your inbox per 24 hours. "
            "Finish or discard some drafts in the app first."
        ),
        "scope_not_authorized": (
            f"The token does not carry {SCOPE}. {REVIEW_HINT} "
            "Once it is approved: python upload_tiktok.py --login"
        ),
        "access_token_invalid": "TikTok token expired. Re-run: python upload_tiktok.py --login",
        "access_token_expired": "TikTok token expired. Re-run: python upload_tiktok.py --login",
        "rate_limit_exceeded": "TikTok rate limit hit (6 uploads per minute). Wait and retry.",
        "invalid_client": (
            "TikTok rejected the client key/secret. Check secrets/tiktok_client.json against "
            "the app in developers.tiktok.com."
        ),
        "invalid_grant": (
            "TikTok rejected the code. The redirect URI and code_verifier must match the ones "
            "used to build the consent URL — run the login again."
        ),
        "unaudited_client_can_only_post_to_private_accounts": (
            f"The app is still unaudited. {REVIEW_HINT}"
        ),
    }
    if code in hints:
        return hints[code]
    bits = [f"TikTok API error {http_status}"]
    if code:
        bits.append(f"({code})")
    if message:
        bits.append(message)
    return " ".join(bits)


def new_verifier() -> str:
    return "".join(secrets.choice(VERIFIER_CHARS) for _ in range(VERIFIER_LEN))


def code_challenge(verifier: str) -> str:
    """Hex, not base64url. TikTok is explicit about this and rejects the RFC form."""
    return hashlib.sha256(verifier.encode("utf-8")).hexdigest()


def authorize_url(state: str, challenge: str = "", *, redirect_uri: str = "") -> str:
    fields = {
        "client_key": _client_key(),
        "scope": SCOPE,
        "response_type": "code",
        "redirect_uri": redirect_uri or _redirect_uri(),
        "state": state,
    }
    if challenge:
        fields["code_challenge"] = challenge
        fields["code_challenge_method"] = "S256"
    return f"{AUTH_URL}?{urllib.parse.urlencode(fields)}"


def _store_token(payload: dict[str, Any]) -> dict[str, Any]:
    access = str(payload.get("access_token") or "")
    if not access:
        raise RuntimeError("TikTok did not return an access_token")
    record = {
        "access_token": access,
        "refresh_token": str(payload.get("refresh_token") or ""),
        "refresh_expires_at": int(time.time()) + int(payload.get("refresh_expires_in") or 0),
        "open_id": str(payload.get("open_id") or ""),
        "scope": str(payload.get("scope") or ""),
        "token_type": str(payload.get("token_type") or "Bearer"),
        "expires_at": int(time.time()) + int(payload.get("expires_in") or 0),
        "saved_at": int(time.time()),
    }
    _write_token(record)
    return record


def exchange_code(code: str, *, verifier: str = "", redirect_uri: str = "") -> dict[str, Any]:
    fields = {
        "client_key": _client_key(),
        "client_secret": _client_secret(),
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri or _redirect_uri(),
    }
    if verifier:
        fields["code_verifier"] = verifier
    return _store_token(_post_form(TOKEN_URL, fields))


def code_from_redirect(text: str) -> str:
    """Accept a bare code or the whole redirected URL pasted back from the browser."""
    value = text.strip()
    if not value:
        raise ValueError("no code provided")
    if "://" in value or value.startswith("?") or "code=" in value:
        query = urllib.parse.urlparse(value).query or value.lstrip("?")
        params = urllib.parse.parse_qs(query)
        found = (params.get("code") or [""])[0]
        if not found:
            error = (params.get("error_description") or params.get("error") or [""])[0]
            raise ValueError(error or "no code in that URL")
        return urllib.parse.unquote(found)
    return urllib.parse.unquote(value)


PAGE_OK = (
    "<html><body style='font:16px -apple-system,sans-serif;padding:40px'>"
    "<h2>TikTok authorized</h2><p>Close this tab and go back to the terminal.</p>"
    "</body></html>"
)
PAGE_BAD = (
    "<html><body style='font:16px -apple-system,sans-serif;padding:40px'>"
    "<h2>Authorization failed</h2><p>{detail}</p>"
    "</body></html>"
)


class _CallbackServer(http.server.HTTPServer):
    """Holds the single captured result so the handler can stay tiny."""

    allow_reuse_address = True
    expected_state = ""
    captured: dict[str, str] = {}


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    server: _CallbackServer  # type: ignore[assignment]

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        first = lambda name: (params.get(name) or [""])[0]  # noqa: E731
        code = first("code")
        error = first("error_description") or first("error")
        state = first("state")

        if code and self.server.expected_state and state != self.server.expected_state:
            error, code = "state mismatch — start the login again", ""

        if code:
            self._reply(200, PAGE_OK)
            self.server.captured = {"code": urllib.parse.unquote(code), "scopes": first("scopes")}
        else:
            self._reply(400, PAGE_BAD.format(detail=error or "no code in the callback"))
            self.server.captured = {"error": error or "no code in the callback"}

    def _reply(self, code: int, body: str) -> None:
        blob = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def log_message(self, *_args: Any) -> None:
        """Keep the one-shot server quiet; the CLI does the talking."""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _bind_callback(redirect_uri: str) -> tuple[_CallbackServer, str]:
    """Serve the registered callback path, resolving a '*' port to a real one."""
    parsed = urllib.parse.urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    raw_port = (parsed.netloc.rsplit(":", 1)[-1] if ":" in parsed.netloc else "")
    port = _free_port() if raw_port in ("", "*") else int(raw_port)
    bind_host = "127.0.0.1" if host in ("localhost", "127.0.0.1") else host
    try:
        server = _CallbackServer((bind_host, port), _CallbackHandler)
    except OSError as exc:
        raise RuntimeError(
            f"Could not listen on {host}:{port} for the TikTok callback ({exc}). "
            "Free the port, or register http://127.0.0.1:*/callback/ and retry."
        ) from exc
    actual = server.server_address[1]
    resolved = urllib.parse.urlunparse(
        (parsed.scheme or "http", f"{host}:{actual}", parsed.path or "/", "", "", "")
    )
    return server, resolved


def _serve_once(server: _CallbackServer, *, state: str, timeout: float) -> str:
    server.expected_state = state
    server.captured = {}
    server.timeout = timeout
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout)
    captured = dict(server.captured)
    server.server_close()
    if captured.get("error"):
        raise RuntimeError(f"TikTok authorization failed: {captured['error']}")
    if not captured.get("code"):
        raise RuntimeError(
            f"Timed out after {int(timeout)}s waiting for the TikTok callback. "
            "Run the login again, or use --paste if the browser cannot reach this machine."
        )
    return captured["code"]


def login(
    *,
    prompt: Callable[[str], str] | None = None,
    paste: bool = False,
    open_browser: bool = True,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """Loopback capture when the redirect is 127.0.0.1, else paste the redirected URL."""
    if not configured():
        raise RuntimeError(SETUP_HINT)

    redirect = _redirect_uri()
    verifier = new_verifier()
    challenge = code_challenge(verifier)
    state = secrets.token_urlsafe(16)
    use_loopback = is_loopback(redirect) and not paste

    server: _CallbackServer | None = None
    if use_loopback:
        server, redirect = _bind_callback(redirect)

    url = authorize_url(state, challenge, redirect_uri=redirect)
    print("\nApprove access for your TikTok account:\n", flush=True)
    print(f"  {url}\n", flush=True)

    if server is not None:
        print(f"Waiting for the callback on {redirect} …", flush=True)
        if open_browser:
            webbrowser.open(url)
        code = _serve_once(server, state=state, timeout=timeout)
    else:
        ask = prompt or input
        code = code_from_redirect(ask("Paste the redirected URL (or just the code): "))

    record = exchange_code(code, verifier=verifier, redirect_uri=redirect)
    scopes = [s.strip() for s in str(record.get("scope") or "").split(",") if s.strip()]
    return {
        "ok": True,
        "openId": record.get("open_id"),
        "scopes": scopes,
        "uploadScope": SCOPE in scopes,
        "tokenFile": str(token_path()),
        "redirectUri": redirect,
    }


def access_token() -> str:
    record = _read_token()
    if not record.get("access_token"):
        raise RuntimeError("TikTok is not authorized yet. Run: python upload_tiktok.py --login")
    # Refresh a minute early so a long upload does not start on a dying token.
    if int(record.get("expires_at") or 0) - 60 > int(time.time()):
        return str(record["access_token"])
    refresh = str(record.get("refresh_token") or "")
    if not refresh:
        raise RuntimeError("TikTok token expired. Run: python upload_tiktok.py --login")
    payload = _post_form(
        TOKEN_URL,
        {
            "client_key": _client_key(),
            "client_secret": _client_secret(),
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        },
    )
    return str(_store_token(payload)["access_token"])


def chunk_plan(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) per TikTok's media transfer rules."""
    if size <= 0:
        raise ValueError("empty video")
    if size <= MAX_CHUNK:
        return size, 1
    chunk = DEFAULT_CHUNK
    total = size // chunk
    if total > MAX_CHUNKS:
        chunk = min(MAX_CHUNK, int(math.ceil(size / MAX_CHUNKS)))
        chunk = max(chunk, MIN_CHUNK)
        total = max(1, size // chunk)
    return chunk, int(total)


def _put_chunk(upload_url: str, blob: bytes, *, first: int, last: int, total: int) -> None:
    req = urllib.request.Request(
        upload_url,
        data=blob,
        method="PUT",
        headers={
            "Content-Type": "video/mp4",
            "Content-Length": str(len(blob)),
            "Content-Range": f"bytes {first}-{last}/{total}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as res:
            res.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"TikTok chunk upload failed ({exc.code}): {detail[:300]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"TikTok chunk upload failed: {exc.reason}") from exc


def upload_draft(
    video: Path,
    *,
    on_progress: Callable[[int], None] | None = None,
    wait: bool = True,
    timeout: float = 300.0,
    require_scope: bool = True,
) -> dict[str, Any]:
    """Send an mp4 to the creator's TikTok inbox as an unfinished draft."""
    video = video.resolve()
    if not video.is_file():
        raise FileNotFoundError(video)
    if require_scope and authorized() and not upload_scope_granted():
        raise RuntimeError(
            f"This token has {granted_scopes() or ['no scopes']}, not {SCOPE}. {REVIEW_HINT} "
            "Pass --no-require-scope to try the call anyway."
        )
    size = video.stat().st_size
    chunk_size, total_chunks = chunk_plan(size)
    token = access_token()

    init = _post_json(
        INBOX_INIT_URL,
        {
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": chunk_size,
                "total_chunk_count": total_chunks,
            }
        },
        token=token,
    )
    data = init.get("data") or {}
    publish_id = str(data.get("publish_id") or "")
    upload_url = str(data.get("upload_url") or "")
    if not publish_id or not upload_url:
        raise RuntimeError("TikTok did not return publish_id / upload_url")

    with video.open("rb") as handle:
        for index in range(total_chunks):
            first = index * chunk_size
            # The final chunk absorbs any trailing bytes.
            last = size - 1 if index == total_chunks - 1 else first + chunk_size - 1
            handle.seek(first)
            blob = handle.read(last - first + 1)
            _put_chunk(upload_url, blob, first=first, last=last, total=size)
            if on_progress is not None:
                on_progress(int(round((last + 1) / size * 100)))

    result = {
        "publishId": publish_id,
        "mode": "draft",
        "status": "PROCESSING_UPLOAD",
        "chunks": total_chunks,
    }
    if wait:
        result.update(wait_for_status(publish_id, timeout=timeout))
    return result


def fetch_status(publish_id: str) -> dict[str, Any]:
    payload = _post_json(STATUS_URL, {"publish_id": publish_id}, token=access_token())
    return payload.get("data") or {}


def wait_for_status(publish_id: str, *, timeout: float = 300.0, interval: float = 3.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    last = "PROCESSING_UPLOAD"
    while time.time() < deadline:
        data = fetch_status(publish_id)
        last = str(data.get("status") or last)
        if last in DONE_STATES:
            return {"status": last}
        if last in FAILED_STATES:
            reason = str(data.get("fail_reason") or "unknown")
            raise RuntimeError(f"TikTok processing failed: {reason}")
        time.sleep(interval)
    return {"status": last, "timedOut": True}
