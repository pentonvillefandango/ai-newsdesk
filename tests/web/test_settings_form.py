from __future__ import annotations

from collections.abc import Collection, Mapping

import pytest

from ai_newsdesk.runtime_settings import RuntimeSettings
from ai_newsdesk.web.forms import SettingsForm, parse_settings_form
from tests.llm.test_validate import CATALOGUE, PRICES

KEYS = {"anthropic", "openai", "openrouter"}


def parse(
    form: Mapping[str, str],
    *,
    current: RuntimeSettings | None = None,
    keys: Collection[str] = KEYS,
) -> SettingsForm:
    return parse_settings_form(
        form,
        current=current or RuntimeSettings(),
        prices=PRICES,
        catalogue=CATALOGUE,
        keys=keys,
    )


VALID = {
    "runs_per_day": "4",
    "briefing_time": "07:00",
    "daily_review_cap": "500",
    "monthly_budget_usd": "5.00",
    "judge_provider": "anthropic",
    "judge_model": "claude-haiku-5-5",
    "writer_model": "claude-haiku-5-5",
    "briefing_model": "claude-sonnet-5-5",
    "show_threshold": "4",
    "why_it_matters": "on",
    "hn_min_points": "100",
}


def test_valid_form() -> None:
    form = parse(VALID)
    assert form.ok
    assert form.values == {
        "runs_per_day": 4,
        "briefing_time": "07:00",
        "daily_review_cap": 500,
        "monthly_budget_usd": 5.0,
        "judge_provider": "anthropic",
        "judge_model": "claude-haiku-5-5",
        "writer_model": "claude-haiku-5-5",
        "briefing_model": "claude-sonnet-5-5",
        "show_threshold": 4,
        "why_it_matters": True,
        "hn_min_points": 100,
    }


def test_unchecked_box_means_off() -> None:
    form = parse({k: v for k, v in VALID.items() if k != "why_it_matters"})
    assert form.values["why_it_matters"] is False


def test_each_field_is_validated() -> None:
    cases = {
        "runs_per_day": ("5", "Choose one of the listed frequencies."),
        "briefing_time": ("25:00", "Use the 24-hour form HH:MM, for example 07:00."),
        "daily_review_cap": ("5001", "Enter a whole number from 0 to 5000."),
        "monthly_budget_usd": ("nan", "Enter an amount from 0 to 100 dollars."),
        "show_threshold": ("11", "Enter a whole number from 0 to 10."),
        "hn_min_points": ("-1", "Enter a whole number from 0 to 10000."),
        "judge_provider": ("mistral", "Choose a judge provider."),
        "writer_model": ("jev-1.13.0", "Choose a Claude model from the list."),
        "briefing_model": ("gpt-5", "Choose a Claude model from the list."),
    }
    for field, (bad, message) in cases.items():
        form = parse({**VALID, field: bad})
        assert form.errors == {field: message}, field


def test_judge_model_must_match_the_provider() -> None:
    form = parse({**VALID, "judge_provider": "typesafe"})
    assert form.errors == {"judge_model": "Choose a model that the selected judge provider offers."}
    form = parse({**VALID, "judge_provider": "typesafe", "judge_model": "jev-1.13.0"})
    assert form.ok


JOBS = ("judge", "writer", "briefing", "consolidator")


@pytest.mark.parametrize("job", JOBS)
@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("anthropic", "claude-sonnet-5-5"),
        ("openai", "gpt-5.4-nano"),
        ("openrouter", "google/gemini-3.8-flash"),
        ("openrouter", "unbiased/pareto-26.9"),  # manual entry
    ],
)
def test_every_job_takes_any_usable_provider_and_model(job: str, provider: str, model: str) -> None:
    form = parse({**VALID, f"{job}_provider": provider, f"{job}_model": f"  {model} "})
    assert form.ok, form.errors
    assert (form.values[f"{job}_provider"], form.values[f"{job}_model"]) == (provider, model)


@pytest.mark.parametrize("job", JOBS)
@pytest.mark.parametrize(
    ("provider", "model", "message"),
    [
        ("openai", "gpt-unknown", "no known price"),
        ("openrouter", "nvidia/nemotron-3.5-lightning:free", "isn't in the OpenRouter model list"),
        ("openrouter", "nobody/unknown", "isn't in the OpenRouter model list"),
        ("anthropic", "google/gemini-3.8-flash", "Choose a Claude model from the list."),
    ],
)
def test_every_job_refuses_unusable_models(
    job: str, provider: str, model: str, message: str
) -> None:
    form = parse({**VALID, f"{job}_provider": provider, f"{job}_model": model})
    assert set(form.errors) == {f"{job}_model"}
    assert message in form.errors[f"{job}_model"]


@pytest.mark.parametrize("job", JOBS)
def test_every_job_names_the_missing_key(job: str) -> None:
    form = parse(
        {**VALID, f"{job}_provider": "openrouter", f"{job}_model": "google/gemini-3.8-flash"},
        keys={"anthropic", "openai"},
    )
    assert form.errors == {f"{job}_provider": "Add your OpenRouter API key first."}


@pytest.mark.parametrize("job", ["writer", "briefing", "consolidator"])
def test_job_providers_are_checked(job: str) -> None:
    form = parse({**VALID, f"{job}_provider": "typesafe", f"{job}_model": "jev-1.13.0"})
    assert form.errors == {f"{job}_provider": "Choose a provider from the list."}


def test_typesafe_judge_keeps_its_own_check_and_needs_no_key() -> None:
    form = parse({**VALID, "judge_provider": "typesafe", "judge_model": "jev-1.13.0"}, keys=set())
    assert form.ok


def test_a_job_missing_from_the_form_is_left_as_it_is() -> None:
    """Today's page posts no provider fields and no consolidator model: those keep their saved
    values, and a posted model is checked against the saved provider."""
    current = RuntimeSettings(
        writer_provider="openrouter",
        writer_model="google/gemini-3.8-flash",
        consolidator_provider="openai",
        consolidator_model="gpt-5.4-nano",
    )
    form = parse(VALID, current=current)
    assert form.errors == {"writer_model": "This model isn't in the OpenRouter model list."}
    form = parse({**VALID, "writer_model": "unbiased/pareto-26.9"}, current=current)
    assert form.ok
    assert form.values["writer_model"] == "unbiased/pareto-26.9"
    for key in ("writer_provider", "consolidator_provider", "consolidator_model"):
        assert key not in form.values
