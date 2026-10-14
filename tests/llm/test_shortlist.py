"""The curated model shortlist in config/models.yaml."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_newsdesk.llm.shortlist import Shortlist
from ai_newsdesk.pricing import Pricing
from tests.conftest import ROOT

MODELS_YAML = ROOT / "config" / "models.yaml"


def test_shipped_shortlist_matches_the_spec() -> None:
    shortlist = Shortlist.load(MODELS_YAML)
    assert shortlist.ids("anthropic") == [
        "claude-haiku-5-5",
        "claude-sonnet-5-5",
        "claude-opus-5-5",
    ]
    assert shortlist.ids("openai") == ["gpt-6-luna", "gpt-5.4-nano", "gpt-5.6-luna", "gpt-6.1-sol"]
    assert shortlist.ids("openrouter") == [
        "anthropic/claude-haiku-5.5",
        "anthropic/claude-sonnet-5.5",
        "anthropic/claude-opus-5.5",
        "openai/gpt-6-luna",
        "openai/gpt-5.4-nano",
        "openai/gpt-5.6-luna",
        "openai/gpt-6.1-sol",
        "google/gemini-3.8-flash",
        "google/gemini-4-argon",
        "deepseek/deepseek-v4.1-flash",
        "deepseek/deepseek-v4-pro-0813",
        "qwen/qwen3.8-flash",
        "qwen/qwen3.8-max-0902",
        "mistralai/mistral-small-2603",
        "mistralai/mistral-large-4-0",
        "mistralai/mistral-medium-3-5",
        "x-ai/grok-4.7",
        "meta-llama/llama-4-maverick",
        "z-ai/glm-5.3-flash",
        "z-ai/glm-5.3",
        "moonshotai/kimi-k2.6",
        "minimax/minimax-m3",
        "nvidia/nemotron-3.5-lightning",
        "nvidia/nemotron-3-super-120b-a12b",
        "cohere/command-a-plus",
    ]
    assert shortlist.ids("typesafe") == []


def test_shortlisted_anthropic_models_are_priced() -> None:
    pricing = Pricing.load(ROOT / "config" / "pricing.yaml")
    assert set(Shortlist.load(MODELS_YAML).ids("anthropic")) <= set(pricing.models("anthropic"))


def test_returned_lists_are_copies() -> None:
    shortlist = Shortlist.load(MODELS_YAML)
    shortlist.ids("openai").append("changed")
    assert "changed" not in shortlist.ids("openai")


@pytest.mark.parametrize(
    "text",
    [
        "anthropic: [a, a]\nopenai: []\nopenrouter: []\n",  # duplicate id
        "anthropic: []\nopenai: []\n",  # a provider missing
        "anthropic: []\nopenai: []\nopenrouter: []\ngemini: [x]\n",  # unknown provider
        "anthropic: ['']\nopenai: []\nopenrouter: []\n",  # empty id
    ],
)
def test_bad_files_are_rejected(tmp_path: Path, text: str) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(text)
    with pytest.raises(ValidationError):
        Shortlist.load(path)
