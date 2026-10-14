"""The Models section's rows, shortlists and datalists, built from settings values."""

from __future__ import annotations

from ai_newsdesk.llm.prices import PriceBook
from ai_newsdesk.llm.shortlist import Shortlist
from ai_newsdesk.web.model_choices import (
    JOBS,
    JUDGE_WARNING,
    datalists,
    effective_model,
    judge_warning,
    model_rows,
)
from tests.conftest import ROOT
from tests.llm.test_validate import CATALOGUE, PRICING

SHORTLIST = Shortlist.load(ROOT / "config" / "models.yaml")
PRICES = PriceBook(PRICING, lambda: CATALOGUE)
DEFAULTS = {
    f"{job}_{field}": value
    for job in ("judge", "writer", "briefing", "consolidator")
    for field, value in (("provider", "anthropic"), ("model", "claude-haiku-5-5"))
}


def _rows(values: dict[str, str], prices: PriceBook = PRICES) -> dict[str, object]:
    return {r.job: r for r in model_rows(values, shortlist=SHORTLIST, prices=prices)}


def test_one_row_per_job_with_the_right_providers() -> None:
    rows = model_rows(DEFAULTS, shortlist=SHORTLIST, prices=PRICES)
    assert [(r.job, r.label) for r in rows] == [
        ("judge", "Judge"),
        ("writer", "Summary writer"),
        ("briefing", "Briefing writer"),
        ("consolidator", "Duplicate merger"),
    ]
    assert [j.name for j in JOBS] == [r.job for r in rows]
    assert [p for p, _ in rows[0].providers] == ["anthropic", "openai", "openrouter", "typesafe"]
    assert dict(rows[0].providers)["typesafe"] == "TypeSafe (Jev)"
    for row in rows[1:]:
        assert [p for p, _ in row.providers] == ["anthropic", "openai", "openrouter"]


def test_shortlists_show_prices_and_mark_unpriced_models() -> None:
    judge = model_rows(DEFAULTS, shortlist=SHORTLIST, prices=PRICES)[0]
    groups = {g.provider: g for g in judge.groups}
    assert list(groups) == ["anthropic", "openai", "openrouter", "typesafe"]
    claude = {o.id: o for o in groups["anthropic"].options}
    assert claude["claude-haiku-5-5"].label == (
        "claude-haiku-5-5 ($0.1 in, $0.5 out per million tokens)"
    )
    openai = {o.id: o for o in groups["openai"].options}
    assert [o.id for o in groups["openai"].options] == SHORTLIST.ids("openai")
    assert openai["gpt-5.4-nano"].usable
    assert openai["gpt-5.4-nano"].label == "gpt-5.4-nano ($0.2 in, $1.25 out per million tokens)"
    assert not openai["gpt-6-luna"].usable  # not in this catalogue
    assert openai["gpt-6-luna"].label == "gpt-6-luna (no price yet)"
    openrouter = {o.id: o for o in groups["openrouter"].options}
    assert len(openrouter) == 25
    assert openrouter["google/gemini-3.8-flash"].usable
    assert [o.id for o in groups["typesafe"].options] == ["jev-1.13.0"]
    writer = model_rows(DEFAULTS, shortlist=SHORTLIST, prices=PRICES)[1]
    assert [g.provider for g in writer.groups] == ["anthropic", "openai", "openrouter"]


def test_datalists_per_provider() -> None:
    lists = {d.provider: d for d in datalists(PRICES, CATALOGUE)}
    assert list(lists) == ["anthropic", "openai", "openrouter", "typesafe"]
    assert [o.id for o in lists["openrouter"].options] == CATALOGUE.ids()
    # OpenAI direct: the catalogue's openai/* ids without the prefix
    assert [o.id for o in lists["openai"].options] == ["gpt-5.4-nano"]
    assert [o.id for o in lists["anthropic"].options] == [
        "claude-haiku-5-5",
        "claude-opus-5-5",
        "claude-sonnet-5-5",
    ]
    assert [o.id for o in lists["typesafe"].options] == ["jev-1.13.0"]
    gemini = {o.id: o for o in lists["openrouter"].options}["google/gemini-3.8-flash"]
    assert gemini.label == "Gemini 3.8 Flash ($0.75 in, $3.75 out per million tokens)"
    assert all(o.usable for d in lists.values() for o in d.options)


def test_datalists_without_a_catalogue() -> None:
    lists = {d.provider: d for d in datalists(PriceBook(PRICING), None)}
    assert lists["openrouter"].options == ()
    assert lists["openai"].options == ()


def test_a_shortlisted_model_is_selected_and_a_manual_one_goes_in_the_text_box() -> None:
    rows = _rows(
        {
            **DEFAULTS,
            "writer_provider": "openrouter",
            "writer_model": "google/gemini-3.8-flash",
            "briefing_provider": "openrouter",
            "briefing_model": "unbiased/pareto-26.9",  # in the catalogue, not shortlisted
            "consolidator_provider": "openai",
            "consolidator_model": "gpt-6-luna",  # shortlisted but unpriced: never select it
        }
    )
    writer, briefing, merger = rows["writer"], rows["briefing"], rows["consolidator"]
    assert (writer.provider, writer.selected, writer.other) == (  # type: ignore[attr-defined]
        "openrouter",
        "google/gemini-3.8-flash",
        "",
    )
    assert (briefing.selected, briefing.other) == ("", "unbiased/pareto-26.9")  # type: ignore[attr-defined]
    assert (merger.selected, merger.other) == ("", "gpt-6-luna")  # type: ignore[attr-defined]
    assert rows["judge"].selected == "claude-haiku-5-5"  # type: ignore[attr-defined]


def test_an_echoed_form_keeps_what_was_posted() -> None:
    rows = _rows(
        {
            **DEFAULTS,
            "writer_provider": "openai",
            "writer_model": "claude-sonnet-5-5",
            "writer_model_other": "gpt-typo",
        }
    )
    writer = rows["writer"]
    assert (writer.provider, writer.selected, writer.other) == (  # type: ignore[attr-defined]
        "openai",
        "claude-sonnet-5-5",
        "gpt-typo",
    )


def test_the_text_box_wins_when_filled() -> None:
    form = {"writer_model": "claude-haiku-5-5", "writer_model_other": "  gpt-5.4-nano "}
    assert effective_model(form, "writer") == "gpt-5.4-nano"
    assert effective_model(
        {"writer_model": "claude-haiku-5-5", "writer_model_other": " "}, "writer"
    ) == ("claude-haiku-5-5")
    assert effective_model({}, "writer") == ""


def test_judge_warning_only_off_the_haiku_default() -> None:
    assert JUDGE_WARNING == (
        "Claude Haiku was chosen by testing on 623 labelled items; other judges are untested on"
        " your news and may filter differently."
    )
    assert not judge_warning(DEFAULTS)
    assert judge_warning({**DEFAULTS, "judge_model": "claude-sonnet-5-5"})
    assert judge_warning({**DEFAULTS, "judge_provider": "typesafe", "judge_model": "jev-1.13.0"})
    assert judge_warning(
        {
            **DEFAULTS,
            "judge_provider": "openrouter",
            "judge_model": "",
            "judge_model_other": "anthropic/claude-haiku-5.5",
        }
    )
    assert not judge_warning(
        {**DEFAULTS, "judge_model": "", "judge_model_other": "claude-haiku-5-5"}
    )
