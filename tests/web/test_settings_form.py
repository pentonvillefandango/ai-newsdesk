from __future__ import annotations

from ai_newsdesk.web.forms import parse_settings_form

MODELS = {"anthropic": {"claude-haiku-5-5", "claude-sonnet-5-5"}, "typesafe": {"jev-1-13"}}
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
    form = parse_settings_form(VALID, models_by_provider=MODELS)
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
    form = parse_settings_form(
        {k: v for k, v in VALID.items() if k != "why_it_matters"}, models_by_provider=MODELS
    )
    assert form.values["why_it_matters"] is False


def test_each_field_is_validated() -> None:
    cases = {
        "runs_per_day": ("5", "Choose one of the listed frequencies."),
        "briefing_time": ("25:00", "Use the 24-hour form HH:MM, for example 07:00."),
        "daily_review_cap": ("5001", "Enter a whole number from 0 to 5000."),
        "monthly_budget_usd": ("nan", "Enter an amount from 0 to 100 dollars."),
        "show_threshold": ("11", "Enter a whole number from 0 to 10."),
        "hn_min_points": ("-1", "Enter a whole number from 0 to 10000."),
        "judge_provider": ("openai", "Choose a judge provider."),
        "writer_model": ("jev-1-13", "Choose a Claude model from the list."),
        "briefing_model": ("gpt-5", "Choose a Claude model from the list."),
    }
    for field, (bad, message) in cases.items():
        form = parse_settings_form({**VALID, field: bad}, models_by_provider=MODELS)
        assert form.errors == {field: message}, field


def test_judge_model_must_match_the_provider() -> None:
    form = parse_settings_form({**VALID, "judge_provider": "typesafe"}, models_by_provider=MODELS)
    assert form.errors == {"judge_model": "Choose a model that the selected judge provider offers."}
    form = parse_settings_form(
        {**VALID, "judge_provider": "typesafe", "judge_model": "jev-1-13"},
        models_by_provider=MODELS,
    )
    assert form.ok
