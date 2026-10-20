"""ElevenLabsNarrator: the only client of the ElevenLabs REST API (briefing read aloud).

No retries inside a call (the scheduler retries). The key is awaited once per call and is never
logged or put in an error message: errors carry only a status code or an exception class name.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from ai_newsdesk.models import NarrationResult, QuotaInfo, VoiceInfo

log = logging.getLogger(__name__)

_VOICE_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")
_NO_KEY = "No ElevenLabs key."


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _Subscription(_Lenient):
    tier: str
    character_count: int
    character_limit: int
    next_character_count_reset_unix: int | None = None


class ElevenLabsNarrator:
    def __init__(
        self,
        http: httpx.AsyncClient,
        key: Callable[[], Awaitable[str]],
        *,
        base_url: str = "https://api.elevenlabs.io",
        timeout_s: float = 180.0,
    ) -> None:
        self._http = http
        self._key = key
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s

    def __repr__(self) -> str:
        return f"ElevenLabsNarrator(base_url={self._base!r})"

    async def narrate(
        self, text_chunks: list[str], *, voice_id: str, model_id: str, credit_factor: float
    ) -> NarrationResult:
        if not _VOICE_ID.match(voice_id):
            return NarrationResult(error="Invalid voice id.")
        try:
            key = await self._key()
        except Exception as exc:  # the resolver must not break the never-raises contract
            return NarrationResult(error=f"Key lookup failed ({type(exc).__name__}).")
        if not key:
            return NarrationResult(error=_NO_KEY)
        url = f"{self._base}/v1/text-to-speech/{quote(voice_id, safe='')}"
        headers = {"xi-api-key": key, "Accept": "audio/mpeg"}
        parts: list[bytes] = []
        request_ids: list[str] = []
        sent = 0  # characters of chunks that were sent (and so possibly billed)

        def billed(chars: int) -> int:
            return math.ceil(chars * credit_factor)

        for i, chunk in enumerate(text_chunks):
            body: dict[str, Any] = {"text": chunk, "model_id": model_id}
            if i:
                if request_ids:
                    body["previous_request_ids"] = request_ids[-3:]
                else:
                    body["previous_text"] = text_chunks[i - 1][-500:]
            try:
                resp = await self._http.post(
                    url,
                    params={"output_format": "mp3_44100_64"},
                    headers=headers,
                    json=body,
                    timeout=self._timeout,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                sent += len(chunk)
                log.warning("ElevenLabs narration failed: %s", type(exc).__name__)
                return NarrationResult(
                    characters_billed=billed(sent),
                    maybe_billed=True,
                    error=f"ElevenLabs request failed ({type(exc).__name__}).",
                )
            if not resp.is_success:
                log.warning("ElevenLabs narration answered %s", resp.status_code)
                error = f"ElevenLabs answered {resp.status_code}."
                if resp.status_code >= 500:
                    sent += len(chunk)
                    return NarrationResult(
                        characters_billed=billed(sent), maybe_billed=True, error=error
                    )
                return NarrationResult(
                    characters_billed=billed(sent), maybe_billed=i > 0, error=error
                )
            sent += len(chunk)
            parts.append(resp.content)
            rid = resp.headers.get("request-id")
            if rid:
                request_ids.append(rid)
        return NarrationResult(audio=b"".join(parts), characters_billed=billed(sent))

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any | None:
        try:
            key = await self._key()
            if not key:
                return None
            resp = await self._http.get(
                f"{self._base}{path}",
                params=params,
                headers={"xi-api-key": key},
                timeout=self._timeout,
            )
            if not resp.is_success:
                log.warning("ElevenLabs GET %s answered %s", path, resp.status_code)
                return None
            return resp.json()
        except Exception as exc:  # any failure is None; never log exception text
            log.warning("ElevenLabs GET %s failed: %s", path, type(exc).__name__)
            return None

    async def voices(self) -> list[VoiceInfo] | None:
        out: list[VoiceInfo] = []
        token: str | None = None
        for _ in range(50):  # hard stop against a looping token
            params: dict[str, Any] = {"page_size": 100}
            if token:
                params["next_page_token"] = token
            data = await self._get_json("/v2/voices", params)
            if not isinstance(data, dict):
                return None
            try:
                for v in data.get("voices") or []:
                    labels = v.get("labels")
                    clean = (
                        {
                            k: x
                            for k, x in labels.items()
                            if isinstance(k, str) and isinstance(x, str)
                        }
                        if isinstance(labels, dict)
                        else {}
                    )
                    out.append(
                        VoiceInfo(
                            voice_id=v["voice_id"],
                            name=v.get("name") or v["voice_id"],
                            category=v.get("category"),
                            labels=clean,
                            preview_url=v.get("preview_url"),
                        )
                    )
            except (KeyError, TypeError, AttributeError, ValidationError):
                return None
            token = data.get("next_page_token")
            if not (data.get("has_more") and isinstance(token, str) and token):
                return out
        return None

    async def model_factors(self) -> dict[str, float] | None:
        data = await self._get_json("/v1/models")
        if not isinstance(data, list):
            return None
        out: dict[str, float] = {}
        try:
            for m in data:
                if not m.get("can_do_text_to_speech"):
                    continue
                rates = m.get("model_rates") or {}
                factor = rates.get("character_cost_multiplier", 1.0)
                out[m["model_id"]] = float(factor)
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        return out

    async def quota(self) -> QuotaInfo | None:
        data = await self._get_json("/v1/user/subscription")
        if not isinstance(data, dict):
            return None
        try:
            sub = _Subscription.model_validate(data)
        except ValidationError:
            return None
        reset = sub.next_character_count_reset_unix
        return QuotaInfo(
            tier=sub.tier,
            character_count=sub.character_count,
            character_limit=sub.character_limit,
            resets_at=datetime.fromtimestamp(reset, UTC) if reset else None,
        )
