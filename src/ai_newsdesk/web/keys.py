"""Provider tests for API keys entered on the Settings page.

Storage is not here: step 1's `ApiKeys` (runtime_settings.py) is the only key store (master plan
R3). Testers make one cheap, side-effect-free call with the candidate key and return None when
it works, else a short message. Callers scrub the key out of any message before showing it.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Awaitable, Callable
from typing import Any

import anthropic
import httpx

from ai_newsdesk.config import Settings
from ai_newsdesk.runtime_settings import ApiKeyName, RuntimeSettings

KEY_LABELS: dict[ApiKeyName, str] = {
    "anthropic": "Anthropic",
    "research_engine": "research-engine",
    "typesafe": "TypeSafe",
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
}
KeyTester = Callable[[str, RuntimeSettings], Awaitable[str | None]]
TEST_TIMEOUT_S = 15.0
MAX_KEY_CHARS = 500
REDACTED = "[key removed]"
_KEY_LIKE = re.compile(r"\b(?:sk|ts)-[A-Za-z0-9_\-]{4,}|[A-Za-z0-9_\-]{32,}")


def scrub(message: str, key: str) -> str:
    """Remove the key (and anything key-shaped) from a provider message before showing it."""
    out = message.replace(key, REDACTED) if key else message
    return _KEY_LIKE.sub(REDACTED, out)[:300]


async def check_anthropic_key(
    key: str, runtime: RuntimeSettings, *, http_client: Any | None = None
) -> str | None:
    """`count_tokens` costs nothing and proves the key and the writer model are usable.

    `http_client` is for tests: the anthropic SDK (>= 1.13) sends through `httpx2`, which respx
    does not patch, so tests pass an `httpx2.AsyncClient` with a `MockTransport`.
    """
    client = anthropic.AsyncAnthropic(
        api_key=key, max_retries=0, timeout=TEST_TIMEOUT_S, http_client=http_client
    )
    try:
        await client.messages.count_tokens(
            model=runtime.writer_model,
            messages=[{"role": "user", "content": "ping"}],
        )
    except anthropic.AuthenticationError:
        return "Anthropic rejected the key (401)."
    except anthropic.PermissionDeniedError:
        return "Anthropic accepted the key but refused access (403)."
    except anthropic.APIStatusError as exc:
        return f"Anthropic answered {exc.status_code}: {exc.message}"
    except anthropic.APIConnectionError:
        return "Could not reach the Anthropic API. Check the network and try again."
    finally:
        await client.close()
    return None


def research_engine_checker(base_url: str, http: httpx.AsyncClient) -> KeyTester:
    """research-engine's /health is open, so it cannot prove a key. GET /v1/engines needs the
    key and has no side effects."""
    url = base_url.rstrip("/") + "/v1/engines"

    async def check(key: str, runtime: RuntimeSettings) -> str | None:
        try:
            response = await http.get(url, headers={"X-API-Key": key}, timeout=TEST_TIMEOUT_S)
        except httpx.HTTPError as exc:
            return f"Could not reach research-engine ({type(exc).__name__})."
        if response.status_code == 401:
            return "research-engine rejected the key (401)."
        if response.status_code != 200:
            return f"research-engine answered {response.status_code}."
        return None

    return check


async def check_typesafe_key(key: str, runtime: RuntimeSettings) -> str | None:
    """One single-question Noul request (optional `jev` extra)."""
    try:
        sdk: Any = importlib.import_module("typesafe_sdk")
    except ImportError:
        return "The TypeSafe SDK isn't installed. Rebuild the image with the jev extra."
    try:
        async with sdk.AsyncTypeSafeClient(api_key=key) as client:
            await client.system_one(
                state={"document": "ping"},
                questions={"ping": sdk.Noul(instructions="Is this text a test message?")},
            )
    except Exception as exc:
        return f"TypeSafe call failed: {type(exc).__name__}: {exc}"
    return None


def default_key_testers(settings: Settings, http: httpx.AsyncClient) -> dict[ApiKeyName, KeyTester]:
    return {
        "anthropic": check_anthropic_key,
        "research_engine": research_engine_checker(settings.research_engine_url, http),
        "typesafe": check_typesafe_key,
    }
