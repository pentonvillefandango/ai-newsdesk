"""Near-duplicate title matching.

Exact URL duplicates never get this far: Store.add_candidates skips canonical URLs it already
has. This module catches the same story told with a slightly different headline, so a new item
can be linked to the story an earlier item already belongs to.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

DEDUPE_WINDOW_HOURS = 72
NEAR_DUPLICATE_JACCARD = 0.8

STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "its",
        "new",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "with",
    }
)
# Words and numbers; dots and hyphens between alphanumerics stay inside the token (gpt-5.5).
_TOKEN = re.compile(r"[a-z0-9]+(?:[.\-][a-z0-9]+)*")


def title_tokens(title: str) -> frozenset[str]:
    text = unicodedata.normalize("NFKC", title).lower()
    return frozenset(
        tok
        for tok in _TOKEN.findall(text)
        if tok not in STOPWORDS and (len(tok) > 1 or tok.isdigit())
    )


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def is_near_duplicate(a: str, b: str, threshold: float = NEAR_DUPLICATE_JACCARD) -> bool:
    return jaccard(title_tokens(a), title_tokens(b)) >= threshold


def find_story_link(
    item_id: int,
    title: str,
    recent: Iterable[tuple[int, str, int | None]],
    threshold: float = NEAR_DUPLICATE_JACCARD,
) -> int | None:
    """Story ID of the most similar recent item that already has a story, else None."""
    tokens = title_tokens(title)
    best_story: int | None = None
    best_score = 0.0
    for other_id, other_title, story_id in recent:
        if other_id == item_id or story_id is None:
            continue
        score = jaccard(tokens, title_tokens(other_title))
        if score >= threshold and score > best_score:
            best_story, best_score = story_id, score
    return best_story
