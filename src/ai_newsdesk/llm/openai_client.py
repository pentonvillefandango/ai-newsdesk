"""OpenAI and OpenRouter behind ModelClient, through the official openai SDK (spec 3.1).

Structured output is a strict json_schema response format built from the pydantic model, and
the answer is validated with the same model. OpenRouter calls also require every parameter to
be supported, cap the price at the catalogue price and ask for the actual cost back.
"""

from __future__ import annotations

from typing import Any

import httpx
import openai
from pydantic import BaseModel, ValidationError

from ai_newsdesk.llm.base import StructuredResult, describe_error, prompt_tokens
from ai_newsdesk.llm.prices import PriceBook
from ai_newsdesk.models import Purpose, Usage
from ai_newsdesk.pricing import UnknownModelError

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# OpenRouter's app attribution: the public repo, never a lab hostname.
OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://github.com/pentonvillefandango/ai-newsdesk",
    "X-Title": "ai-newsdesk",
}
PROVIDERS = ("openai", "openrouter")


def make_sdk(
    provider: str,
    api_key: str,
    *,
    http_client: httpx.AsyncClient | None = None,
    max_retries: int = 2,
) -> openai.AsyncOpenAI:
    """An SDK client for OpenAI direct (default base URL) or OpenRouter."""
    if provider not in PROVIDERS:
        raise ValueError(f"unsupported provider for the OpenAI-compatible client: {provider}")
    # The SDK types this as its own HTTP client's class; an httpx client behaves the same at run
    # time (tests pass one with a MockTransport).
    http: Any = http_client
    if provider == "openrouter":
        return openai.AsyncOpenAI(
            api_key=api_key,
            base_url=OPENROUTER_BASE_URL,
            default_headers=OPENROUTER_HEADERS,
            http_client=http,
            max_retries=max_retries,
        )
    return openai.AsyncOpenAI(api_key=api_key, http_client=http, max_retries=max_retries)


def _strict(node: Any) -> Any:
    if isinstance(node, list):
        return [_strict(v) for v in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:  # strict mode allows no keywords beside a $ref
        return {"$ref": node["$ref"]}
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "default":
            continue
        if key in ("properties", "$defs"):  # maps of names: recurse into values only
            out[key] = {name: _strict(v) for name, v in value.items()}
        else:
            out[key] = _strict(value)
    if "properties" in out or out.get("type") == "object":
        if "properties" not in out or not isinstance(out.get("additionalProperties", False), bool):
            # A dict[str, X] field: strict mode cannot express it, and closing it would allow
            # only {}. Fail loudly rather than send a schema that silently changes meaning.
            raise ValueError("strict schemas cannot contain free-form objects (dict fields)")
        out["additionalProperties"] = False
        out["required"] = list(out.get("properties", {}))
    return out


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The model's JSON schema in strict form: every object closed, every property required."""
    return _strict(model.model_json_schema())


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


class OpenAICompatibleClient:
    def __init__(
        self, client: openai.AsyncOpenAI, provider: str, model: str, prices: PriceBook
    ) -> None:
        if provider not in PROVIDERS:
            raise ValueError(f"unsupported provider for the OpenAI-compatible client: {provider}")
        self._client = client
        self.provider = provider
        self.model = model
        self._prices = prices

    def worst_case_usd(self, *, system: str, user: str, max_tokens: int) -> float:
        return self._prices.worst_case_usd(
            self.provider, self.model, prompt_tokens(system, user), max_tokens
        )

    def _usage(self, response: Any, purpose: Purpose, fallback: tuple[int, int]) -> Usage:
        u = getattr(response, "usage", None)
        if u is None:  # never record a paid call as free: assume the worst case
            return Usage(
                provider=self.provider,
                model=self.model,
                purpose=purpose,
                input_tokens=fallback[0],
                output_tokens=fallback[1],
            )
        details = getattr(u, "prompt_tokens_details", None)
        cached = _int(getattr(details, "cached_tokens", 0))
        written = _int(getattr(details, "cache_write_tokens", 0))
        reported: float | None = None
        if self.provider == "openrouter":
            cost = getattr(u, "cost", None)
            if isinstance(cost, int | float) and not isinstance(cost, bool) and cost >= 0:
                reported = float(cost)
        return Usage(
            provider=self.provider,
            model=self.model,
            purpose=purpose,
            # OpenAI counts cached tokens inside prompt_tokens; keep them out of input_tokens.
            input_tokens=max(0, _int(u.prompt_tokens) - cached - written),
            output_tokens=_int(u.completion_tokens),
            cache_read_tokens=cached,
            cache_write_tokens=written,
            reported_cost_usd=reported,
        )

    async def structured[T: BaseModel](
        self, *, system: str, user: str, schema: type[T], max_tokens: int, purpose: Purpose
    ) -> StructuredResult[T]:
        extra_body: dict[str, Any] | None = None
        # The output cap, reasoning included. OpenAI direct takes max_completion_tokens; most
        # OpenRouter endpoints list only max_tokens, and require_parameters would refuse to
        # route a parameter an endpoint does not list.
        openrouter = self.provider == "openrouter"
        if openrouter:
            try:
                price = self._prices.price(self.provider, self.model)
            except UnknownModelError:  # never route a call without a price ceiling
                return StructuredResult(error="no catalogue price for this model")
            extra_body = {
                "provider": {
                    "require_parameters": True,
                    "max_price": {
                        "prompt": price.input_per_mtok,
                        "completion": price.output_per_mtok,
                    },
                },
                "usage": {"include": True},
            }
        try:
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema.__name__,
                        "strict": True,
                        "schema": strict_schema(schema),
                    },
                },
                max_tokens=max_tokens if openrouter else openai.omit,
                max_completion_tokens=openai.omit if openrouter else max_tokens,
                extra_body=extra_body,
            )
        except Exception as exc:  # never raises: the caller's items fail
            return StructuredResult(error=describe_error(exc))
        usage = self._usage(response, purpose, (prompt_tokens(system, user), max_tokens))
        if not response.choices:
            return StructuredResult(usage=usage, error="no choices in the response")
        choice = response.choices[0]
        finish = choice.finish_reason
        if choice.message.refusal or finish == "content_filter":
            return StructuredResult(usage=usage, refused=True, error="refused")
        if finish not in ("stop", None):
            return StructuredResult(usage=usage, error=f"unusable (finish_reason={finish!s:.40})")
        try:
            parsed = schema.model_validate_json(choice.message.content or "")
        except ValidationError:
            return StructuredResult(usage=usage, error="output does not match the schema")
        return StructuredResult(parsed=parsed, usage=usage)
