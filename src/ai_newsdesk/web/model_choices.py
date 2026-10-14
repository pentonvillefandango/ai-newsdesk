"""The Settings page's Models section (multi-provider spec 3.5): one row per AI job, each with a
provider select, a shortlist select and an "Other model" text box with a per-provider datalist.

Pure functions over the settings values, the shortlist and the PriceBook; the template only
renders what is built here. Without JavaScript every control submits, and the text box wins
when it is filled (`effective_model`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ai_newsdesk.llm.catalogue import Catalogue
from ai_newsdesk.llm.prices import Price, PriceBook
from ai_newsdesk.llm.shortlist import Shortlist
from ai_newsdesk.pricing import UnknownModelError

PROVIDER_NAMES = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
    "typesafe": "TypeSafe (Jev)",
}
MODEL_PROVIDERS = ("anthropic", "openai", "openrouter")
JUDGE_PROVIDERS = (*MODEL_PROVIDERS, "typesafe")
DEFAULT_JUDGE = ("anthropic", "claude-haiku-5-5")
JUDGE_WARNING = (
    "Claude Haiku was chosen by testing on 623 labelled items; other judges are untested on"
    " your news and may filter differently."
)


@dataclass(frozen=True)
class Job:
    name: str
    label: str
    hint: str
    providers: tuple[str, ...]


JOBS = (
    Job("judge", "Judge", "Scores every new item and hides the junk.", JUDGE_PROVIDERS),
    Job("writer", "Summary writer", "Writes each story's summary.", MODEL_PROVIDERS),
    Job("briefing", "Briefing writer", "Writes the morning briefing.", MODEL_PROVIDERS),
    Job(
        "consolidator",
        "Duplicate merger",
        "Checks whether new stories repeat earlier ones.",
        MODEL_PROVIDERS,
    ),
)
JOB_NAMES = tuple(job.name for job in JOBS)


@dataclass(frozen=True)
class ModelOption:
    id: str
    label: str
    usable: bool  # priced; an unpriced shortlist entry is shown but cannot be chosen


@dataclass(frozen=True)
class OptionGroup:
    provider: str
    label: str
    options: tuple[ModelOption, ...]


@dataclass(frozen=True)
class ModelRow:
    job: str
    label: str
    hint: str
    providers: tuple[tuple[str, str], ...]  # (value, label)
    provider: str
    groups: tuple[OptionGroup, ...]
    selected: str  # the shortlist option to select ("" = "Other model")
    other: str  # the text box's value


def price_text(price: Price) -> str:
    return f"${price.input_per_mtok:g} in, ${price.output_per_mtok:g} out per million tokens"


def _price(prices: PriceBook, provider: str, model: str) -> Price | None:
    try:
        return prices.price(provider, model)
    except UnknownModelError:
        return None


def _option(prices: PriceBook, provider: str, model: str, name: str | None = None) -> ModelOption:
    price = _price(prices, provider, model)
    if price is None:
        return ModelOption(model, f"{name or model} (no price yet)", False)
    return ModelOption(model, f"{name or model} ({price_text(price)})", True)


def shortlist_ids(provider: str, *, shortlist: Shortlist, prices: PriceBook) -> list[str]:
    """The curated ids for a provider; Jev's come from pricing.yaml."""
    if provider == "typesafe":
        return prices.pricing.models("typesafe")
    return shortlist.ids(provider)


def option_groups(
    providers: tuple[str, ...], *, shortlist: Shortlist, prices: PriceBook
) -> tuple[OptionGroup, ...]:
    return tuple(
        OptionGroup(
            provider,
            PROVIDER_NAMES[provider],
            tuple(
                _option(prices, provider, model)
                for model in shortlist_ids(provider, shortlist=shortlist, prices=prices)
            ),
        )
        for provider in providers
    )


def datalists(prices: PriceBook, catalogue: Catalogue | None) -> tuple[OptionGroup, ...]:
    """Autocomplete for the "Other model" box, one list per provider: every usable model.
    OpenAI direct lists the catalogue's openai/* ids without the prefix."""
    catalogue_ids = catalogue.ids() if catalogue is not None else []
    ids = {
        "anthropic": sorted(prices.pricing.models("anthropic")),
        "openai": sorted(
            {m.removeprefix("openai/") for m in catalogue_ids if m.startswith("openai/")}
            | set(prices.pricing.models("openai"))
        ),
        "openrouter": catalogue_ids,
        "typesafe": sorted(prices.pricing.models("typesafe")),
    }
    groups: list[OptionGroup] = []
    for provider, models in ids.items():
        options: list[ModelOption] = []
        for model in models:
            entry = catalogue.get(model) if catalogue is not None else None
            option = _option(prices, provider, model, entry.name if entry else None)
            if option.usable:
                options.append(option)
        groups.append(OptionGroup(provider, PROVIDER_NAMES[provider], tuple(options)))
    return tuple(groups)


def effective_model(form: Mapping[str, str], job: str) -> str:
    """The "Other model" text box wins when it is filled; else the shortlist select."""
    other = form.get(f"{job}_model_other", "").strip()
    return other or form.get(f"{job}_model", "").strip()


def model_rows(
    values: Mapping[str, str], *, shortlist: Shortlist, prices: PriceBook
) -> list[ModelRow]:
    """`values` are the saved settings, or an echoed form (which has the `_model_other` keys).

    A saved model that is not a usable shortlist option goes in the text box, so saving an
    unchanged page always posts the saved provider and model back.
    """
    rows: list[ModelRow] = []
    for job in JOBS:
        provider = values.get(f"{job.name}_provider", "")
        model = values.get(f"{job.name}_model", "")
        groups = option_groups(job.providers, shortlist=shortlist, prices=prices)
        if f"{job.name}_model_other" in values:
            selected, other = model, values[f"{job.name}_model_other"]
        else:
            usable = {o.id for g in groups if g.provider == provider for o in g.options if o.usable}
            selected, other = (model, "") if model in usable else ("", model)
        rows.append(
            ModelRow(
                job=job.name,
                label=job.label,
                hint=job.hint,
                providers=tuple((p, PROVIDER_NAMES[p]) for p in job.providers),
                provider=provider,
                groups=groups,
                selected=selected,
                other=other,
            )
        )
    return rows


def judge_warning(values: Mapping[str, str]) -> bool:
    """Shown when the judge is anything but the tested default, Claude Haiku on Anthropic."""
    return (values.get("judge_provider", ""), effective_model(values, "judge")) != DEFAULT_JUDGE
