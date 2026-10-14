import re
from datetime import date
from pathlib import Path

import pytest

from ai_newsdesk.briefing import (
    BRIEFING_SYSTEM,
    briefing_date_label,
    clean_briefing,
    render_briefing_request,
)
from ai_newsdesk.models import BriefingStory, Category, Purpose, WriteInput
from ai_newsdesk.pricing import Pricing
from ai_newsdesk.writer import WRITER_SYSTEM, BriefingDoc, HaikuWriter, StoryBatch, StoryWriting
from tests.fake_anthropic import FakeAnthropic, as_client, rate_limited, reply

PRICING = Pricing.load(Path(__file__).resolve().parents[1] / "config" / "pricing.yaml")
MODEL = "claude-haiku-5-5"


def wi(story_id: int, *, include_why: bool = True, texts: list[str] | None = None) -> WriteInput:
    return WriteInput(
        story_id=story_id,
        headline=f"Story {story_id}",
        texts=texts or [f"Title {story_id}\n\nBody {story_id}."],
        include_why=include_why,
    )


def sw(
    story_id: int, summary: str = "It happened.", why: str | None = "It matters."
) -> StoryWriting:
    return StoryWriting(story_id=story_id, summary=summary, why_it_matters=why)


def bs(story_id: int, url: str, category: Category = Category.RESEARCH) -> BriefingStory:
    return BriefingStory(
        story_id=story_id,
        headline=f"Story {story_id}",
        category=category,
        summary="Summary.",
        url=url,
    )


async def test_write_stories_request_shape() -> None:
    fake = FakeAnthropic([reply(StoryBatch(stories=[sw(7)]))])
    writer = HaikuWriter(as_client(fake), MODEL, PRICING)
    texts, usage = await writer.write_stories([wi(7, texts=["a", "b", "c"])])
    call = fake.messages.calls[0]
    assert call["output_format"] is StoryBatch
    assert call["output_config"] == {"effort": "low"}
    assert call["system"] == [
        {"type": "text", "text": WRITER_SYSTEM, "cache_control": {"type": "ephemeral"}}
    ]
    assert "temperature" not in call
    content = call["messages"][0]["content"]
    assert '<story id="7">' in content
    assert '"c"' not in content  # at most two texts
    assert [(t.story_id, t.summary, t.why_it_matters) for t in texts] == [
        (7, "It happened.", "It matters.")
    ]
    assert usage[0].purpose is Purpose.WRITE


async def test_include_why_false_forces_none() -> None:
    fake = FakeAnthropic([reply(StoryBatch(stories=[sw(1, why="Should be dropped.")]))])
    texts, _ = await HaikuWriter(as_client(fake), MODEL, PRICING).write_stories(
        [wi(1, include_why=False)]
    )
    assert texts[0].why_it_matters is None


async def test_batches_of_ten_and_bad_entries_dropped() -> None:
    first = StoryBatch(stories=[sw(i) for i in range(1, 11)] + [sw(99), sw(1, "duplicate")])
    second = StoryBatch(stories=[sw(11, summary="   "), sw(12)])
    fake = FakeAnthropic([reply(first), reply(second)])
    texts, usage = await HaikuWriter(as_client(fake), MODEL, PRICING).write_stories(
        [wi(i) for i in range(1, 13)]
    )
    assert len(fake.messages.calls) == 2
    assert [t.story_id for t in texts] == [*range(1, 11), 12]
    assert texts[0].summary == "It happened."
    assert len(usage) == 2


async def test_refusal_and_errors_never_raise() -> None:
    fake = FakeAnthropic(
        [reply(StoryBatch(stories=[sw(1)]), stop_reason="refusal"), rate_limited()]
    )
    writer = HaikuWriter(as_client(fake), MODEL, PRICING, batch_size=1)
    texts, usage = await writer.write_stories([wi(1), wi(2)])
    assert texts == []
    assert len(usage) == 1


async def test_write_briefing_uses_briefing_model_and_cleans_links() -> None:
    body = (
        "Quiet day.\n\n## Research\n\n"
        "[A result](https://example.com/a) and [a fake](https://evil.example/x)."
    )
    fake = FakeAnthropic([reply(BriefingDoc(body=body))])
    writer = HaikuWriter(as_client(fake), MODEL, PRICING, briefing_model="claude-sonnet-5-5")
    text, usage = await writer.write_briefing(
        [bs(1, "https://example.com/a")], "Wednesday 14 October 2026"
    )
    call = fake.messages.calls[0]
    assert call["model"] == "claude-sonnet-5-5"
    assert call["output_format"] is BriefingDoc
    assert call["system"][0]["text"] == BRIEFING_SYSTEM
    assert "Wednesday 14 October 2026" in call["messages"][0]["content"]
    assert text is not None
    assert "[A result](https://example.com/a)" in text
    assert "evil.example" not in text and "a fake" in text
    assert (usage[0].model, usage[0].purpose) == ("claude-sonnet-5-5", Purpose.BRIEFING)


async def test_write_briefing_failures_return_none() -> None:
    fake = FakeAnthropic([reply(None, stop_reason="max_tokens"), rate_limited()])
    writer = HaikuWriter(as_client(fake), MODEL, PRICING)
    stories = [bs(1, "https://example.com/a")]
    assert (await writer.write_briefing(stories, "x"))[0] is None
    assert await writer.write_briefing(stories, "x") == (None, [])
    assert await writer.write_briefing([], "x") == (None, [])


def test_worst_case_costs() -> None:
    writer = HaikuWriter(
        as_client(FakeAnthropic([])), MODEL, PRICING, briefing_model="claude-sonnet-5-5"
    )
    assert writer.worst_case_cost_stories([wi(1)]) > PRICING.cost(MODEL, 0, 4000)
    assert writer.worst_case_cost_stories([]) == 0.0
    assert writer.worst_case_cost_briefing([bs(1, "https://example.com/a")]) > PRICING.cost(
        "claude-sonnet-5-5", 0, 4000
    )


def test_worst_case_stories_is_an_absolute_upper_bound() -> None:
    # Haiku: $0.10/Mtok in (cache write 1.25x = $0.125), $0.50/Mtok out, max_tokens 4000.
    # Multi-byte text proves input is counted in UTF-8 bytes, not characters.
    writer = HaikuWriter(as_client(FakeAnthropic([])), MODEL, PRICING)
    batch = [wi(1, texts=["é" * 500])]
    sent = WRITER_SYSTEM + '<story id="1">' + "é" * 500  # a lower bound on what is sent
    min_bytes = len(sent.encode("utf-8")) + 600
    floor = min_bytes * 0.125 / 1e6 + 4000 * 0.50 / 1e6
    got = writer.worst_case_cost_stories(batch)
    assert got >= floor
    assert got == pytest.approx(floor, abs=1e-3)  # and not wildly above it
    # two batches of one story each cost twice one batch
    two = HaikuWriter(as_client(FakeAnthropic([])), MODEL, PRICING, batch_size=1)
    assert two.worst_case_cost_stories([wi(1), wi(2)]) == pytest.approx(
        2 * two.worst_case_cost_stories([wi(1)]), rel=0.01
    )


def test_worst_case_briefing_is_an_absolute_upper_bound() -> None:
    # Sonnet: $2.00/Mtok in (cache write 1.25x = $2.50), $10.00/Mtok out, max_tokens 4000.
    writer = HaikuWriter(
        as_client(FakeAnthropic([])), MODEL, PRICING, briefing_model="claude-sonnet-5-5"
    )
    stories = [bs(1, "https://example.com/é")]
    sent = BRIEFING_SYSTEM + render_briefing_request(stories, "Wednesday 14 October 2026")
    min_bytes = len(sent.encode("utf-8")) + 600
    floor = min_bytes * 2.50 / 1e6 + 4000 * 10.00 / 1e6
    got = writer.worst_case_cost_briefing(stories)
    assert got >= floor
    assert got == pytest.approx(floor, abs=1e-3)


def test_clean_briefing_keeps_only_allowed_links() -> None:
    body = "[ok](https://a.example/1) [bad](javascript:alert) [gone](https://b.example/2)"
    assert clean_briefing(body, {"https://a.example/1"}) == "[ok](https://a.example/1) bad gone"


def test_clean_briefing_keeps_the_markdown_lite_format() -> None:
    body = (
        "Overview of the day.  \r\n\r\n\r\n## Model releases\r\n\r\n"
        "[Haiku 5.5](https://a.example/1) ships.\n\n\n\n## Research\n\nA [paper](https://a.example/2)."
    )
    allowed = {"https://a.example/1", "https://a.example/2"}
    assert clean_briefing(body, allowed) == (
        "Overview of the day.\n\n## Model releases\n\n[Haiku 5.5](https://a.example/1) ships."
        "\n\n## Research\n\nA [paper](https://a.example/2)."
    )


def test_date_label() -> None:
    assert briefing_date_label(date(2026, 10, 14)) == "Wednesday 14 October 2026"


def test_briefing_prompt_states_the_format() -> None:
    for phrase in ("600 words", "## ", "untrusted data", "Model releases", "url"):
        assert phrase in BRIEFING_SYSTEM


def test_writer_prompt_states_the_rules() -> None:
    for phrase in ("3 sentences", "include_why", "untrusted data", "null"):
        assert phrase in WRITER_SYSTEM


def test_briefing_story_payload_is_escaped() -> None:
    text = render_briefing_request([bs(1, "https://example.com/<a>")], "today")
    assert "<a>" not in text
    assert '<story id="1">' in text


def _surviving_urls(text: str) -> set[str]:
    return {m.group(2) for m in re.finditer(r"\[([^\]\n]*)\]\(([^)\s]+)\)", text)}


def test_clean_briefing_nested_brackets_cannot_smuggle_a_link() -> None:
    allowed = {"https://a.example/1"}
    out = clean_briefing("[[t](https://x.example/1)](https://evil.example/p)", allowed)
    assert _surviving_urls(out) == set()
    assert "evil.example" not in out or "](" not in out
    deep = clean_briefing(
        "[[[t](https://e1.example)](https://e2.example)](https://e3.example)", allowed
    )
    assert _surviving_urls(deep) == set()


def test_clean_briefing_nested_story_link_still_works() -> None:
    allowed = {"https://a.example/1"}
    out = clean_briefing("[[t](https://a.example/1)](https://evil.example/p)", allowed)
    assert _surviving_urls(out) <= allowed
    assert "[t](https://a.example/1)" in out


def test_clean_briefing_valid_link_is_unchanged() -> None:
    body = "See [the paper](https://a.example/1) today."
    assert clean_briefing(body, {"https://a.example/1"}) == body
