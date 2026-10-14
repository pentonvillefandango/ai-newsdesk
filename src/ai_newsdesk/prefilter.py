# src/ai_newsdesk/prefilter.py
"""Free heuristic pre-filter: decides which new items are worth a paid review.

score = trust weight x source feedback weight + HN points bonus + multi-source bonus
        - blocklist penalty

X items need no extra handling here: the x_person adapter already keeps only posts whose URL
handle equals the source's handle, so reposts and echo accounts never reach this point.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ai_newsdesk.dedupe import NEAR_DUPLICATE_JACCARD, jaccard, title_tokens
from ai_newsdesk.models import SourceConfig, SourceKind, Trust

TRUST_WEIGHT: dict[Trust, float] = {Trust.PRIMARY: 3.0, Trust.TRUSTED: 2.0, Trust.DISCOVERY: 1.0}
DROP_BELOW = 1.0
HN_POINTS_PER_BONUS = 200.0
HN_BONUS_MAX = 2.0
MULTI_SOURCE_BONUS = 1.0
MULTI_SOURCE_BONUS_MAX = 2.0
BLOCKLIST_PENALTY = 10.0
ARXIV_KEYWORDS_KEY = "keywords"
BLOCKLIST_KEY = "blocklist"  # settings key, a JSON list of lower-case keywords or phrases


class SettingsReader(Protocol):
    async def get_setting(self, key: str) -> Any: ...


async def load_blocklist(store: SettingsReader) -> list[str]:
    """The `blocklist` settings key as a list of non-empty strings ([] when unset or malformed)."""
    value = await store.get_setting(BLOCKLIST_KEY)
    if not isinstance(value, list):
        return []
    return [str(v).strip().lower() for v in value if str(v).strip()]


@dataclass(frozen=True)
class PrefilterInput:
    item_id: int
    source: SourceConfig
    title: str
    author: str | None = None
    snippet: str = ""
    hn_points: int | None = None


@dataclass(frozen=True)
class Decision:
    item_id: int
    score: float
    keep: bool
    reason: str  # "ok" | "below_threshold" | "blocklist" | "arxiv_no_keyword"


def _contains_phrase(text: str, phrase: str) -> bool:
    phrase = phrase.strip().lower()
    if not phrase:
        return False
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text.lower()) is not None


def blocklist_hits(inp: PrefilterInput, blocklist: Sequence[str]) -> list[str]:
    text = f"{inp.title}\n{inp.snippet}"
    return [kw for kw in blocklist if _contains_phrase(text, kw)]


def arxiv_keywords(source: SourceConfig) -> list[str]:
    value = source.config.get(ARXIV_KEYWORDS_KEY, [])
    return [str(v) for v in value] if isinstance(value, list) else []


def arxiv_allowed(inp: PrefilterInput) -> bool:
    text = f"{inp.title}\n{inp.author or ''}"
    return any(_contains_phrase(text, kw) for kw in arxiv_keywords(inp.source))


def heuristic_score(inp: PrefilterInput, other_sources: int, blocklist: Sequence[str]) -> float:
    score = TRUST_WEIGHT[inp.source.trust] * inp.source.weight
    if inp.hn_points:
        score += min(HN_BONUS_MAX, inp.hn_points / HN_POINTS_PER_BONUS)
    score += min(MULTI_SOURCE_BONUS_MAX, MULTI_SOURCE_BONUS * max(0, other_sources))
    score -= BLOCKLIST_PENALTY * len(blocklist_hits(inp, blocklist))
    return round(score, 3)


def decide(inp: PrefilterInput, other_sources: int, blocklist: Sequence[str]) -> Decision:
    score = heuristic_score(inp, other_sources, blocklist)
    if inp.source.kind == SourceKind.ARXIV and not arxiv_allowed(inp):
        return Decision(inp.item_id, score, False, "arxiv_no_keyword")
    if blocklist_hits(inp, blocklist):
        return Decision(inp.item_id, score, False, "blocklist")
    if score < DROP_BELOW:
        return Decision(inp.item_id, score, False, "below_threshold")
    return Decision(inp.item_id, score, True, "ok")


def corroboration(
    rows: Sequence[PrefilterInput], linked: set[int] | frozenset[int] = frozenset()
) -> dict[int, int]:
    """Per item: other distinct sources in this batch with a near-duplicate title, +1 if linked."""
    tokens = [title_tokens(r.title) for r in rows]
    others: dict[int, set[int]] = {r.item_id: set() for r in rows}
    for i, a in enumerate(rows):
        for j in range(i + 1, len(rows)):
            b = rows[j]
            if a.source.id == b.source.id:
                continue
            if jaccard(tokens[i], tokens[j]) >= NEAR_DUPLICATE_JACCARD:
                others[a.item_id].add(b.source.id)
                others[b.item_id].add(a.source.id)
    return {
        item_id: len(srcs) + (1 if item_id in linked else 0) for item_id, srcs in others.items()
    }
