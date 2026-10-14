"""The Compare page's setup form (multi-provider spec 4): job, 2-4 models, sample size and the
spend cap. Returns clean values plus field errors; never raises.

The model rows are Settings' model controls named m1..m4 (`<key>_provider`, `<key>_model`,
`<key>_model_other`); a row whose provider is "Not used" (empty) is skipped.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field

from ai_newsdesk import compare
from ai_newsdesk.llm.catalogue import Catalogue
from ai_newsdesk.llm.prices import PriceBook
from ai_newsdesk.llm.validate import PROVIDER_LABELS, validate_model_choice
from ai_newsdesk.store.records import CompareJob, CompareModel
from ai_newsdesk.web.model_choices import effective_model

MODEL_KEYS = tuple(f"m{n}" for n in range(1, compare.MAX_MODELS + 1))
JOB_ERROR = "Choose Judge or Summary writer."
MODELS_ERROR = "Choose at least two models."
DUPLICATE_ERROR = "This model is already in this comparison."
CAP_ERROR = f"Enter an amount from ${compare.CAP_MIN:.2f} to ${compare.CAP_MAX:.2f}."


@dataclass
class CompareSetup:
    job: CompareJob | None = None
    models: list[CompareModel] = field(default_factory=list)
    sample_size: int = 0
    cap_usd: float = compare.CAP_DEFAULT
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def key_error(provider: str, keys: Collection[str]) -> str | None:
    """Every provider needs its key here, Anthropic too: a call without one can only fail."""
    if provider in PROVIDER_LABELS and provider not in keys:
        return f"Add your {PROVIDER_LABELS[provider]} API key first."
    return None


def parse_compare_form(
    form: Mapping[str, str],
    *,
    prices: PriceBook,
    catalogue: Catalogue | None,
    keys: Collection[str],
    money_left: float,
) -> CompareSetup:
    """`keys` holds the names of the keys that resolve; `money_left` is this month's."""
    setup = CompareSetup()
    errors = setup.errors
    job = form.get("job", "")
    if job == "judge" or job == "writer":
        setup.job = job
    else:
        errors["job"] = JOB_ERROR

    seen: set[str] = set()
    for key in MODEL_KEYS:
        provider = form.get(f"{key}_provider", "").strip()
        if not provider:
            continue
        model = effective_model(form, key)
        error = validate_model_choice(provider, model, pricebook=prices, catalogue=catalogue)
        if error is not None:
            errors[f"{key}_model"] = error
            continue
        if (missing := key_error(provider, keys)) is not None:
            errors[f"{key}_provider"] = missing
            continue
        choice = CompareModel(provider=provider, model=model)
        if choice.key in seen:
            errors[f"{key}_model"] = DUPLICATE_ERROR
            continue
        seen.add(choice.key)
        setup.models.append(choice)
    used = sum(1 for key in MODEL_KEYS if form.get(f"{key}_provider", "").strip())
    if used < compare.MIN_MODELS:
        errors["models"] = MODELS_ERROR

    raw_size = form.get("sample_size", "").strip()
    if setup.job is not None:
        top = compare.SAMPLE_MAX[setup.job]
        if not raw_size:
            setup.sample_size = compare.SAMPLE_DEFAULT[setup.job]
        elif raw_size.isdigit() and 1 <= int(raw_size) <= top:
            setup.sample_size = int(raw_size)
        else:
            errors["sample_size"] = f"Enter a whole number from 1 to {top}."

    raw_cap = form.get("cap_usd", "").strip().removeprefix("$")
    if raw_cap:
        try:
            cap = float(raw_cap)
        except ValueError:
            cap = math.nan
        if not (math.isfinite(cap) and compare.CAP_MIN <= round(cap, 2) <= compare.CAP_MAX):
            errors["cap_usd"] = CAP_ERROR
        else:
            setup.cap_usd = round(cap, 2)
    if "cap_usd" not in errors and setup.cap_usd > money_left:
        errors["cap_usd"] = (
            f"You have ${money_left:.2f} left this month, so the cap can't be higher."
        )
    return setup
