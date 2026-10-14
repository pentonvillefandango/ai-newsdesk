"""The judge, writer and consolidator run over any ModelClient: the transport is the only thing
a provider changes (multi-provider spec 3.1)."""

from __future__ import annotations

import pytest

from ai_newsdesk.briefing import BRIEFING_SYSTEM, render_briefing_request
from ai_newsdesk.consolidate import CONSOLIDATE_SYSTEM, LLMConsolidator, StoryGroups
from ai_newsdesk.judges.llm import BatchJudgement, ItemJudgement, LLMJudge
from ai_newsdesk.judges.prompts import JUDGE_SYSTEM, render_items
from ai_newsdesk.llm.base import StructuredResult
from ai_newsdesk.models import (
    BriefingStory,
    Category,
    ConsolidationStory,
    Purpose,
    ReviewInput,
    SourceKind,
    Trust,
    WriteInput,
)
from ai_newsdesk.writer import (
    DATE_LABEL_ALLOWANCE,
    WRITER_SYSTEM,
    BriefingDoc,
    LLMWriter,
    StoryBatch,
    StoryWriting,
    render_stories,
)
from tests.fake_model_client import FakeModelClient
from tests.test_consolidate import NOW

PROVIDERS = [("openrouter", "google/gemini-3.8-flash"), ("openai", "gpt-5.4-nano")]


def inp(item_id: int) -> ReviewInput:
    return ReviewInput(
        item_id=item_id,
        source_name="Lab blog",
        source_kind=SourceKind.FEED,
        trust=Trust.PRIMARY,
        title=f"Item {item_id}",
        author=None,
        text="Article text.",
        story_candidates=[],
    )


def verdicts(*ids: int) -> BatchJudgement:
    return BatchJudgement(
        verdicts=[
            ItemJudgement(item_id=i, signal=7, junk=False, category="research", same_story_id=None)
            for i in ids
        ]
    )


def wi(story_id: int) -> WriteInput:
    return WriteInput(
        story_id=story_id, headline=f"Story {story_id}", texts=["Body."], include_why=True
    )


def bs(story_id: int) -> BriefingStory:
    return BriefingStory(
        story_id=story_id,
        headline=f"Story {story_id}",
        category=Category.RESEARCH,
        summary="Summary.",
        url="https://example.com/a",
    )


def cs(story_id: int) -> ConsolidationStory:
    return ConsolidationStory(
        id=story_id,
        headline=f"Story {story_id}",
        category=Category.RESEARCH,
        score=5.0,
        first_seen_at=NOW,
    )


# ---- judge ------------------------------------------------------------------------------------


@pytest.mark.parametrize(("provider", "model"), PROVIDERS)
async def test_judge_runs_over_any_model_client(provider: str, model: str) -> None:
    fake = FakeModelClient([verdicts(1, 2)], provider=provider, model=model)
    judge = LLMJudge(fake)
    assert judge.model == model
    result = await judge.review([inp(1), inp(2)])
    assert [v.item_id for v in result.verdicts] == [1, 2]
    assert result.failed_item_ids == []
    [call] = fake.calls
    assert call["system"] == JUDGE_SYSTEM
    assert call["user"] == render_items([inp(1), inp(2)])
    assert call["schema"] is BatchJudgement
    assert call["max_tokens"] == 4000
    assert call["purpose"] is Purpose.REVIEW
    [u] = result.usage
    assert (u.provider, u.model, u.purpose) == (provider, model, Purpose.REVIEW)


async def test_judge_refused_and_invalid_output_fail_the_batch_but_keep_usage() -> None:
    fake = FakeModelClient(["refused", "invalid"])
    judge = LLMJudge(fake, batch_size=1)
    result = await judge.review([inp(1), inp(2)])
    assert result.verdicts == []
    assert result.failed_item_ids == [1, 2]
    assert [u.provider for u in result.usage] == ["openrouter", "openrouter"]


async def test_judge_transport_errors_and_raising_clients_never_raise() -> None:
    fake = FakeModelClient([StructuredResult(error="HTTP 500"), RuntimeError("boom")])
    result = await LLMJudge(fake, batch_size=1).review([inp(1), inp(2)])
    assert result.failed_item_ids == [1, 2]
    assert result.usage == []


def test_judge_worst_case_comes_from_the_client() -> None:
    fake = FakeModelClient()
    judge = LLMJudge(fake, batch_size=1)
    expected = sum(
        fake.worst_case_usd(system=JUDGE_SYSTEM, user=render_items([i]), max_tokens=4000)
        for i in (inp(1), inp(2))
    )
    assert judge.worst_case_cost([inp(1), inp(2)]) == pytest.approx(expected)
    assert judge.worst_case_cost([]) == 0.0


# ---- writer -----------------------------------------------------------------------------------


@pytest.mark.parametrize(("provider", "model"), PROVIDERS)
async def test_writer_runs_over_any_model_client(provider: str, model: str) -> None:
    stories = FakeModelClient(
        [StoryBatch(stories=[StoryWriting(story_id=1, summary="Did.", why_it_matters="Big.")])],
        provider=provider,
        model=model,
    )
    briefing = FakeModelClient(
        [BriefingDoc(body="Quiet day. [A](https://example.com/a)")],
        provider="anthropic",
        model="claude-sonnet-5-5",
    )
    writer = LLMWriter(stories, briefing)
    assert (writer.model, writer.briefing_model) == (model, "claude-sonnet-5-5")

    texts, usage = await writer.write_stories([wi(1)])
    assert [(t.story_id, t.summary, t.why_it_matters) for t in texts] == [(1, "Did.", "Big.")]
    [call] = stories.calls
    assert (call["system"], call["schema"], call["purpose"]) == (
        WRITER_SYSTEM,
        StoryBatch,
        Purpose.WRITE,
    )
    assert call["user"] == render_stories([wi(1)])
    assert [(u.provider, u.model, u.purpose) for u in usage] == [(provider, model, Purpose.WRITE)]

    body, usage = await writer.write_briefing([bs(1)], "Wednesday 14 October 2026")
    assert body == "Quiet day. [A](https://example.com/a)"
    [bcall] = briefing.calls
    assert (bcall["system"], bcall["schema"], bcall["purpose"]) == (
        BRIEFING_SYSTEM,
        BriefingDoc,
        Purpose.BRIEFING,
    )
    assert [(u.provider, u.purpose) for u in usage] == [("anthropic", Purpose.BRIEFING)]


async def test_writer_failures_keep_usage_and_never_raise() -> None:
    stories = FakeModelClient(["refused", ValueError("boom")])
    briefing = FakeModelClient(["invalid", StructuredResult(error="HTTP 429")])
    writer = LLMWriter(stories, briefing, batch_size=1)
    texts, usage = await writer.write_stories([wi(1), wi(2)])
    assert texts == [] and [u.provider for u in usage] == ["openrouter"]
    body, usage = await writer.write_briefing([bs(1)], "x")
    assert body is None and len(usage) == 1
    assert await writer.write_briefing([bs(1)], "x") == (None, [])


def test_writer_worst_cases_come_from_each_client() -> None:
    stories = FakeModelClient(usd_per_token=1e-6)
    briefing = FakeModelClient(usd_per_token=5e-6)
    writer = LLMWriter(stories, briefing, max_tokens=3000, briefing_max_tokens=2000)
    assert writer.worst_case_cost_stories([wi(1)]) == pytest.approx(
        stories.worst_case_usd(system=WRITER_SYSTEM, user=render_stories([wi(1)]), max_tokens=3000)
    )
    user = render_briefing_request([bs(1)], DATE_LABEL_ALLOWANCE)
    assert writer.worst_case_cost_briefing([bs(1)]) == pytest.approx(
        briefing.worst_case_usd(system=BRIEFING_SYSTEM, user=user, max_tokens=2000)
    )


# ---- consolidator -----------------------------------------------------------------------------


@pytest.mark.parametrize(("provider", "model"), PROVIDERS)
async def test_consolidator_runs_over_any_model_client(provider: str, model: str) -> None:
    fake = FakeModelClient([StoryGroups(groups=[[1, 2]])], provider=provider, model=model)
    c = LLMConsolidator(fake)
    assert c.model == model
    groups, usage = await c.find_groups([cs(1), cs(2)])
    assert groups == [[1, 2]]
    [call] = fake.calls
    assert (call["system"], call["schema"], call["purpose"], call["max_tokens"]) == (
        CONSOLIDATE_SYSTEM,
        StoryGroups,
        Purpose.REVIEW,
        2000,
    )
    assert [(u.provider, u.model) for u in usage] == [(provider, model)]


async def test_consolidator_failures_give_no_groups() -> None:
    fake = FakeModelClient(["refused", RuntimeError("boom")])
    c = LLMConsolidator(fake)
    groups, usage = await c.find_groups([cs(1), cs(2)])
    assert groups == [] and len(usage) == 1
    assert await c.find_groups([cs(1), cs(2)]) == ([], [])
