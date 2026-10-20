"""Validation for the Sources, Settings and Models forms. Returns clean values plus field
errors; never raises."""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ai_newsdesk.llm.catalogue import Catalogue
from ai_newsdesk.llm.prices import PriceBook
from ai_newsdesk.llm.validate import (
    PROVIDER_ERROR,
    PROVIDER_LABELS,
    missing_key_error,
    validate_model_choice,
)
from ai_newsdesk.models import SourceConfig, SourceKind, Trust
from ai_newsdesk.runtime_settings import RuntimeSettings
from ai_newsdesk.web.estimate import RUNS_PER_DAY_OPTIONS
from ai_newsdesk.web.model_choices import effective_model

ConfigValue = str | int | float | bool | list[str]

HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
NAME_MAX = 80
QUERY_MAX = 200
URL_MAX = 2000

# kind -> (config key, field label, hint). arXiv and HN are singletons: name and trust only.
EDITABLE_KINDS: dict[SourceKind, tuple[str, str, str]] = {
    SourceKind.FEED: ("url", "Feed address", "The RSS or Atom address, starting with https://."),
    SourceKind.X_PERSON: (
        "handle",
        "X handle",
        "Without the @. Posts are found through research-engine search; X itself is never fetched.",
    ),
    SourceKind.SEARCH: ("query", "Search words", "A topic search run through research-engine."),
    SourceKind.GITHUB: ("repo", "Repository", "owner/name. New releases are collected."),
}

FEED_ERROR = "Use a feed address that starts with http:// or https://."
HANDLE_ERROR = "An X handle is 1 to 15 letters, digits or underscores."
QUERY_ERROR = f"Enter 1 to {QUERY_MAX} characters to search for."
REPO_ERROR = "Use the form owner/name, for example huggingface/transformers."
NAME_ERROR = f"Give the source a name of 1 to {NAME_MAX} characters."


@dataclass
class SourceForm:
    kind: SourceKind | None
    name: str
    trust: Trust | None
    value: str
    config: dict[str, ConfigValue] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def _value(kind: SourceKind, raw: str) -> tuple[ConfigValue | None, str | None]:
    value = raw.strip()
    if kind == SourceKind.FEED:
        try:
            parts = urlsplit(value)
        except ValueError:
            return None, FEED_ERROR
        if parts.scheme not in ("http", "https") or not parts.netloc or len(value) > URL_MAX:
            return None, FEED_ERROR
        return value, None
    if kind == SourceKind.X_PERSON:
        handle = value.removeprefix("@")
        return (handle, None) if HANDLE_RE.match(handle) else (None, HANDLE_ERROR)
    if kind == SourceKind.SEARCH:
        query = " ".join(value.split())
        return (query, None) if 0 < len(query) <= QUERY_MAX else (None, QUERY_ERROR)
    if kind == SourceKind.GITHUB:
        repo = value.removeprefix("https://github.com/").strip("/")
        return (repo, None) if REPO_RE.match(repo) else (None, REPO_ERROR)
    return None, None


def parse_source_form(
    *, kind: str, name: str, trust: str, value: str, existing: SourceConfig | None = None
) -> SourceForm:
    """Validate an add form, or an edit form when ``existing`` is given (its kind is kept)."""
    errors: dict[str, str] = {}
    source_kind: SourceKind | None
    if existing is not None:
        source_kind = existing.kind
    else:
        try:
            source_kind = SourceKind(kind)
        except ValueError:
            source_kind = None
        if source_kind not in EDITABLE_KINDS:
            source_kind = None
            errors["kind"] = "Choose what kind of source this is."
    try:
        source_trust: Trust | None = Trust(trust)
    except ValueError:
        source_trust = None
        errors["trust"] = "Choose a trust level."
    clean_name = " ".join(name.split())
    if not 0 < len(clean_name) <= NAME_MAX:
        errors["name"] = NAME_ERROR
    config: dict[str, ConfigValue] = dict(existing.config) if existing is not None else {}
    if source_kind is not None and source_kind in EDITABLE_KINDS:
        key = EDITABLE_KINDS[source_kind][0]
        parsed, error = _value(source_kind, value)
        if error is not None:
            errors["value"] = error
        elif parsed is not None:
            config[key] = parsed
    return SourceForm(
        kind=source_kind,
        name=clean_name,
        trust=source_trust,
        value=value.strip(),
        config=config,
        errors=errors,
    )


VOICE_ID_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")
NARRATION_WHEN = (
    ("scheduled", "Ready at briefing time"),
    ("on_demand", "Only when I press Make audio"),
)
NARRATION_MODELS = (
    ("eleven_v4", "Eleven v4 (best)"),
    ("eleven_multilingual_v2", "Multilingual v2 (most stable long-form)"),
    ("eleven_flash_v2_5", "Flash v2.5 (half the characters)"),
)


BRIEFING_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
JUDGE_PROVIDERS = ("anthropic", "typesafe", "openai", "openrouter")
# The jobs whose (provider, model) pair the form may set, besides the judge.
MODEL_JOBS = ("writer", "briefing", "consolidator")


@dataclass
class SettingsForm:
    values: dict[str, object] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def parse_settings_form(form: Mapping[str, str]) -> SettingsForm:
    """Validate the Settings form (schedule, budget, reading). ``values`` holds typed values for
    valid fields only. Model fields are not read: they belong to the Models form."""
    result = SettingsForm()
    values, errors = result.values, result.errors

    def integer(key: str, low: int, high: int, message: str) -> None:
        try:
            number = int(form.get(key, "").strip())
        except ValueError:
            errors[key] = message
            return
        if low <= number <= high:
            values[key] = number
        else:
            errors[key] = message

    try:
        runs = int(form.get("runs_per_day", ""))
    except ValueError:
        runs = -1
    if runs in RUNS_PER_DAY_OPTIONS:
        values["runs_per_day"] = runs
    else:
        errors["runs_per_day"] = "Choose one of the listed frequencies."

    briefing_time = form.get("briefing_time", "").strip()
    if BRIEFING_TIME_RE.match(briefing_time):
        values["briefing_time"] = briefing_time
    else:
        errors["briefing_time"] = "Use the 24-hour form HH:MM, for example 07:00."

    integer("daily_review_cap", 0, 5000, "Enter a whole number from 0 to 5000.")

    try:
        budget = float(form.get("monthly_budget_usd", "").strip())
    except ValueError:
        budget = math.nan
    if math.isfinite(budget) and 0 <= budget <= 100:
        values["monthly_budget_usd"] = round(budget, 2)
    else:
        errors["monthly_budget_usd"] = "Enter an amount from 0 to 100 dollars."

    integer("show_threshold", 0, 10, "Enter a whole number from 0 to 10.")
    integer("hn_min_points", 0, 10000, "Enter a whole number from 0 to 10000.")
    values["why_it_matters"] = form.get("why_it_matters") == "on"

    # The page always posts all four; a form without them (an older client) leaves them as saved.
    if any(key.startswith("narration_") for key in form):
        values["narration_enabled"] = form.get("narration_enabled") == "on"
        when = form.get("narration_when")
        if when in dict(NARRATION_WHEN):
            values["narration_on_demand"] = when == "on_demand"
        elif when is not None:
            errors["narration_when"] = "Choose when audio is made."
        voice = form.get("narration_voice_id", "").strip()
        if VOICE_ID_RE.match(voice):
            values["narration_voice_id"] = voice
        else:
            errors["narration_voice_id"] = "Choose a voice from the list."
        model = form.get("narration_model", "")
        if model in dict(NARRATION_MODELS):
            values["narration_model"] = model
        else:
            errors["narration_model"] = "Choose one of the listed models."
        integer(
            "narration_monthly_chars", 0, 10_000_000, "Enter a whole number from 0 to 10000000."
        )
    return result


def parse_models_form(
    form: Mapping[str, str],
    *,
    current: RuntimeSettings,
    prices: PriceBook,
    catalogue: Catalogue | None,
    keys: Collection[str],
) -> SettingsForm:
    """Validate the Models form: each AI job's (provider, model). ``values`` holds typed values
    for valid fields only.

    Each pair goes through validate_model_choice, and a provider that needs a key must have one
    (``keys``: the names that resolve). A job whose fields are not in the form keeps its saved
    values (``current``); a posted model without a posted provider is checked against the saved
    provider. A filled ``<job>_model_other`` box wins over the ``<job>_model`` select.
    """
    form = {
        **form,
        **{
            f"{job}_model": effective_model(form, job)
            for job in ("judge", *MODEL_JOBS)
            if f"{job}_model_other" in form
        },
    }
    result = SettingsForm()
    values, errors = result.values, result.errors

    def model_choice(job: str, provider: str, model: str) -> None:
        error = validate_model_choice(provider, model, pricebook=prices, catalogue=catalogue)
        if error is not None:
            errors[f"{job}_model"] = error
            return
        values[f"{job}_model"] = model
        key_error = missing_key_error(provider, keys)
        if key_error is not None:
            errors[f"{job}_provider"] = key_error

    provider = form.get("judge_provider", "")
    if provider == "typesafe":  # Jev keeps its own check: a TypeSafe model in pricing.yaml
        values["judge_provider"] = provider
        judge_model = form.get("judge_model", "")
        if judge_model in prices.pricing.models("typesafe"):
            values["judge_model"] = judge_model
        else:
            errors["judge_model"] = "Choose a model that the selected judge provider offers."
    elif provider in JUDGE_PROVIDERS:
        values["judge_provider"] = provider
        model_choice("judge", provider, form.get("judge_model", "").strip())
    else:
        errors["judge_provider"] = "Choose a judge provider."

    for job in MODEL_JOBS:
        if f"{job}_provider" in form:
            provider = form[f"{job}_provider"]
            if provider not in PROVIDER_LABELS:
                errors[f"{job}_provider"] = PROVIDER_ERROR
                continue
            values[f"{job}_provider"] = provider
        else:
            provider = getattr(current, f"{job}_provider")
        if f"{job}_model" in form:
            model_choice(job, provider, form[f"{job}_model"].strip())
        elif f"{job}_provider" in form:  # a new provider with the saved model: check the pair
            model_choice(job, provider, getattr(current, f"{job}_model"))
    return result
