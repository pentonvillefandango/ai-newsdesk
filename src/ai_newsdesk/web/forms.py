"""Validation for the Sources forms. Returns clean values plus field errors; never raises."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ai_newsdesk.models import SourceConfig, SourceKind, Trust
from ai_newsdesk.web.estimate import RUNS_PER_DAY_OPTIONS

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


BRIEFING_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
JUDGE_PROVIDERS = ("anthropic", "typesafe")


@dataclass
class SettingsForm:
    values: dict[str, object] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def parse_settings_form(
    form: Mapping[str, str], *, models_by_provider: Mapping[str, set[str]]
) -> SettingsForm:
    """Validate the runtime-settings form. ``values`` holds typed values for valid fields only."""
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

    provider = form.get("judge_provider", "")
    if provider in JUDGE_PROVIDERS:
        values["judge_provider"] = provider
        judge_model = form.get("judge_model", "")
        if judge_model in models_by_provider.get(provider, set()):
            values["judge_model"] = judge_model
        else:
            errors["judge_model"] = "Choose a model that the selected judge provider offers."
    else:
        errors["judge_provider"] = "Choose a judge provider."

    claude_models = models_by_provider.get("anthropic", set())
    for key in ("writer_model", "briefing_model"):
        model = form.get(key, "")
        if model in claude_models:
            values[key] = model
        else:
            errors[key] = "Choose a Claude model from the list."

    values["why_it_matters"] = form.get("why_it_matters") == "on"
    return result
