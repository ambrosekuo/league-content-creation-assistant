"""OpenAI provider with Structured Outputs (default: gpt-5-nano)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from clip_classifiers.providers.base import build_ai_payload
from clip_classifiers.providers.schema import CLASSIFICATION_JSON_SCHEMA
from clip_classifiers.taxonomy import AI_SYSTEM_PROMPT
from clip_classifiers.types import ClipSignals

DEFAULT_MODEL = "gpt-5-nano"
DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.api_key = (
            api_key
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("OPEN_API_KEY")  # common .env typo
            or ""
        )
        self.model = model or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout

    def classify_metadata(self, clip: dict[str, Any], signals: ClipSignals) -> dict[str, Any]:
        if not self.api_key.strip():
            raise RuntimeError("OPENAI_API_KEY is not set")
        payload = build_ai_payload(clip, signals)
        body: dict[str, Any] = {
            "model": self.model,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "clip_classification",
                    "strict": True,
                    "schema": CLASSIFICATION_JSON_SCHEMA,
                },
            },
            "messages": [
                {"role": "system", "content": AI_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Input:\n{json.dumps(payload, ensure_ascii=False)}",
                },
            ],
        }
        # gpt-5-nano only supports the default temperature; omit unless overridden.
        temp = os.environ.get("OPENAI_TEMPERATURE")
        if temp is not None and temp.strip() != "":
            body["temperature"] = float(temp)
        raw = self._post_json("/chat/completions", body)
        return self._parse_completion(raw)

    def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI API error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenAI API request failed: {exc}") from exc

    def _parse_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError("OpenAI API returned no choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            text = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict)
            )
        else:
            text = str(content or "")
        if not text.strip():
            # Some models return parsed JSON on message.parsed
            parsed = message.get("parsed")
            if isinstance(parsed, dict):
                return parsed
            raise RuntimeError("OpenAI API returned empty content")
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise RuntimeError("OpenAI structured output must be a JSON object")
        return parsed
