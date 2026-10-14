"""Build the ModelClient for one AI job (spec 3.4). Only app.py calls this.

An SdkPool holds the SDK clients for one context build: one per provider, shared by every job
that uses that provider. make_client refuses an unusable choice (unpriced model, missing key)
with UnusableModelError, whose message is safe to log; the caller falls back.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import anthropic
import openai

from ai_newsdesk.llm.anthropic_client import AnthropicClient, Effort
from ai_newsdesk.llm.base import ModelClient
from ai_newsdesk.llm.catalogue import Catalogue
from ai_newsdesk.llm.openai_client import OpenAICompatibleClient, make_sdk
from ai_newsdesk.llm.prices import PriceBook
from ai_newsdesk.llm.validate import missing_key_error, validate_model_choice

# No SDK retries: a failed call is recorded once at its worst case, so a retry would be an
# unrecorded attempt; the pipeline offers failed items again on the next run instead.
MAX_RETRIES = 0
# The SDKs' default is 10 minutes; a run holds the pipeline lock, so bound every call.
CALL_TIMEOUT_S = 120.0


class UnusableModelError(ValueError):
    """The choice cannot be used; the message is for the owner and never holds a key."""


class SdkPool:
    """SDK clients for one context build, created on first use. `keys` maps provider names to
    resolved keys (an empty Anthropic key still builds a client whose calls fail with 401)."""

    def __init__(
        self,
        keys: Mapping[str, str],
        *,
        http_client: Any | None = None,
        max_retries: int = MAX_RETRIES,
        timeout: float = CALL_TIMEOUT_S,
    ) -> None:
        self._keys = dict(keys)
        self._http = http_client  # tests only: a MockTransport client
        self._max_retries = max_retries
        self._timeout = timeout
        self._anthropic: anthropic.AsyncAnthropic | None = None
        self._openai: dict[str, openai.AsyncOpenAI] = {}

    def __repr__(self) -> str:
        return "SdkPool(<redacted>)"

    def has_key(self, provider: str) -> bool:
        return bool(self._keys.get(provider))

    def anthropic(self) -> anthropic.AsyncAnthropic:
        if self._anthropic is None:
            self._anthropic = anthropic.AsyncAnthropic(
                api_key=self._keys.get("anthropic") or "not-set",
                http_client=self._http,
                max_retries=self._max_retries,
                timeout=self._timeout,
            )
        return self._anthropic

    def openai_compatible(self, provider: str) -> openai.AsyncOpenAI:
        if provider not in self._openai:
            self._openai[provider] = make_sdk(
                provider,
                self._keys.get(provider, ""),
                http_client=self._http,
                max_retries=self._max_retries,
                timeout=self._timeout,
            )
        return self._openai[provider]

    async def aclose(self) -> None:
        """Close the SDK clients this pool built (a one-off pool, like the Test button's).
        An injected test http client belongs to its owner and stays open."""
        if self._http is not None:
            return
        if self._anthropic is not None:
            await self._anthropic.close()
        for sdk in self._openai.values():
            await sdk.close()


def make_client(
    provider: str,
    model: str,
    *,
    sdks: SdkPool,
    pricebook: PriceBook,
    catalogue: Catalogue | None,
    effort: Effort = "low",
    cache_system: bool = True,
) -> ModelClient:
    """`effort` and `cache_system` are the Anthropic per-job options; the OpenAI-compatible
    client has no equivalent (spec 3.1) and ignores them."""
    keys = {p for p in ("anthropic", "openai", "openrouter") if sdks.has_key(p)}
    error = validate_model_choice(
        provider, model, pricebook=pricebook, catalogue=catalogue
    ) or missing_key_error(provider, keys)
    if error is not None:
        raise UnusableModelError(error)
    if provider == "anthropic":
        return AnthropicClient(
            sdks.anthropic(), model, pricebook, effort=effort, cache_system=cache_system
        )
    return OpenAICompatibleClient(sdks.openai_compatible(provider), provider, model, pricebook)
