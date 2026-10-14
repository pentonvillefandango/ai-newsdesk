"""Validation for the Sources forms. Returns clean values plus field errors; never raises."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ai_newsdesk.models import SourceConfig, SourceKind, Trust

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
