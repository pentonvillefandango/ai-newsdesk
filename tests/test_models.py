from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ai_newsdesk.models import (
    Candidate,
    Category,
    ItemStatus,
    Purpose,
    ReviewInput,
    SourceConfig,
    SourceKind,
    Story,
    StorySource,
    Trust,
    Usage,
    Verdict,
)


def test_enum_values_are_the_stored_strings() -> None:
    assert [t.value for t in Trust] == ["primary", "trusted", "discovery"]
    assert [k.value for k in SourceKind] == ["feed", "x_person", "search", "arxiv", "github", "hn"]
    assert [c.value for c in Category] == [
        "model_release",
        "research",
        "product",
        "policy",
        "industry",
        "opinion",
        "other",
    ]
    assert [s.value for s in ItemStatus] == [
        "new",
        "dropped",
        "queued",
        "reviewed",
        "review_failed",
        "expired",
    ]
    assert [p.value for p in Purpose] == ["review", "write", "briefing"]
    assert Category("model_release") is Category.MODEL_RELEASE
    assert f"{ItemStatus.QUEUED}" == "queued"


@pytest.mark.parametrize("signal", [0, 4, 10])
def test_verdict_signal_in_range(signal: int) -> None:
    v = Verdict(item_id=1, signal=signal, junk=False, category=Category.RESEARCH)
    assert v.signal == signal
    assert v.same_story_id is None


@pytest.mark.parametrize("signal", [-1, 11])
def test_verdict_signal_out_of_range(signal: int) -> None:
    with pytest.raises(ValidationError):
        Verdict(item_id=1, signal=signal, junk=False, category=Category.RESEARCH)


def test_verdict_category_must_be_known() -> None:
    with pytest.raises(ValidationError):
        Verdict.model_validate({"item_id": 1, "signal": 5, "junk": False, "category": "gossip"})


def test_defaults() -> None:
    src = SourceConfig.model_validate(
        {
            "id": 1,
            "kind": "feed",
            "name": "Lab",
            "config": {"url": "https://e.test/f"},
            "trust": "primary",
        }
    )
    assert (src.enabled, src.weight, src.trust) == (True, 1.0, Trust.PRIMARY)
    cand = Candidate(source_id=1, url="https://e.test/a", title="A")
    assert (cand.snippet, cand.author, cand.hn_points) == ("", None, None)
    usage = Usage(provider="anthropic", model="m", purpose=Purpose.REVIEW, input_tokens=5)
    assert (usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (0, 0, 0)
    ri = ReviewInput(
        item_id=1,
        source_name="Lab",
        source_kind=SourceKind.FEED,
        trust=Trust.PRIMARY,
        title="A",
        text="body",
    )
    assert ri.story_candidates == []


def test_story_holds_sources() -> None:
    at = datetime(2026, 10, 14, 12, tzinfo=UTC)
    story = Story.model_validate(
        {
            "id": 1,
            "headline": "H",
            "category": "research",
            "score": 7.5,
            "summary": None,
            "why_it_matters": None,
            "first_seen_at": at,
            "last_updated_at": at,
            "feedback": 0,
            "sources": [
                {
                    "item_id": 3,
                    "source_name": "Lab",
                    "source_kind": "feed",
                    "url": "https://e.test/a",
                    "title": "A",
                }
            ],
        }
    )
    assert isinstance(story.sources[0], StorySource)
    assert story.sources[0].published_at is None
