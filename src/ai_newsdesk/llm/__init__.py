"""Model clients for every AI job: one ModelClient contract over Anthropic, OpenAI and
OpenRouter, plus the OpenRouter catalogue, provider-aware prices and the curated shortlist."""

from ai_newsdesk.llm.anthropic_client import AnthropicClient, usage_from_response
from ai_newsdesk.llm.base import (
    SCHEMA_OVERHEAD_TOKENS,
    ModelClient,
    StructuredResult,
    describe_error,
    estimate_tokens,
    prompt_tokens,
    safe_json,
)
from ai_newsdesk.llm.catalogue import Catalogue, CatalogueModel, ModelCatalogue
from ai_newsdesk.llm.openai_client import OpenAICompatibleClient, make_sdk
from ai_newsdesk.llm.prices import Price, PriceBook
from ai_newsdesk.llm.shortlist import Shortlist

__all__ = [
    "SCHEMA_OVERHEAD_TOKENS",
    "AnthropicClient",
    "Catalogue",
    "CatalogueModel",
    "ModelCatalogue",
    "ModelClient",
    "OpenAICompatibleClient",
    "Price",
    "PriceBook",
    "Shortlist",
    "StructuredResult",
    "describe_error",
    "estimate_tokens",
    "make_sdk",
    "prompt_tokens",
    "safe_json",
    "usage_from_response",
]
