import pytest

from ai_newsdesk.dedupe import (
    DEDUPE_WINDOW_HOURS,
    NEAR_DUPLICATE_JACCARD,
    find_story_link,
    is_near_duplicate,
    jaccard,
    title_tokens,
)


def test_title_tokens_normalise_case_punctuation_and_stopwords() -> None:
    assert title_tokens("OpenAI Releases GPT-5.5 — With Better Reasoning!") == frozenset(
        {"openai", "releases", "gpt-5.5", "better", "reasoning"}
    )


def test_jaccard_of_empty_set_is_zero() -> None:
    assert jaccard(frozenset(), frozenset({"a"})) == 0.0
    assert jaccard(frozenset({"x", "y"}), frozenset({"x", "y"})) == 1.0


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (
            "OpenAI releases GPT-5.5 with better reasoning",
            "OpenAI Releases GPT-5.5 With Better Reasoning!",
            True,
        ),
        (
            "OpenAI releases GPT-5.5 with better reasoning",
            "OpenAI releases GPT-5 with better reasoning",
            False,
        ),
        ("Anthropic ships Claude Haiku", "Anthropic ships Claude Haiku today", True),  # 4/5 = 0.8
        ("Anthropic ships Claude", "Anthropic ships Claude Haiku", False),  # 3/4 = 0.75
    ],
)
def test_is_near_duplicate(a: str, b: str, expected: bool) -> None:
    assert is_near_duplicate(a, b) is expected


def test_find_story_link_picks_best_linked_match() -> None:
    recent = [
        (1, "Anthropic ships Claude Haiku 5.5 today", 10),
        (2, "Anthropic ships Claude Haiku 5.5", 20),
        (3, "Google updates Gemini app", 30),
    ]
    assert find_story_link(9, "Anthropic Ships Claude Haiku 5.5", recent) == 20


def test_find_story_link_ignores_self_and_items_without_story() -> None:
    recent = [
        (9, "Anthropic ships Claude Haiku 5.5", 99),
        (4, "Anthropic ships Claude Haiku 5.5", None),
    ]
    assert find_story_link(9, "Anthropic ships Claude Haiku 5.5", recent) is None


def test_find_story_link_returns_none_below_threshold() -> None:
    recent = [(1, "Google updates Gemini app with new voices", 5)]
    assert find_story_link(2, "Anthropic ships Claude Haiku 5.5", recent) is None


def test_window_and_threshold_constants() -> None:
    assert DEDUPE_WINDOW_HOURS == 72
    assert NEAR_DUPLICATE_JACCARD == 0.8
