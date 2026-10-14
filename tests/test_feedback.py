# tests/test_feedback.py
from datetime import UTC, datetime
from typing import cast

import pytest

from ai_newsdesk.clock import FixedClock
from ai_newsdesk.feedback import (
    FEEDBACK_DAYS,
    add_blocklist_keyword,
    set_feedback,
    update_source_weights,
    weight_for,
)
from ai_newsdesk.models import Category, SourceConfig, SourceKind, Trust
from ai_newsdesk.store.store import Store
from tests.feedback_stubs import StubStore
from tests.pipeline_helpers import add_item, add_source

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def src(sid: int, weight: float = 1.0, enabled: bool = True) -> SourceConfig:
    return SourceConfig(
        id=sid,
        kind=SourceKind.FEED,
        name=f"s{sid}",
        config={"url": "https://example.com"},
        trust=Trust.TRUSTED,
        enabled=enabled,
        weight=weight,
    )


@pytest.mark.parametrize(
    ("ups", "downs", "expected"),
    [(0, 0, 1.0), (1, 0, 1.1), (0, 1, 0.9), (25, 0, 2.0), (0, 20, 0.3), (12, 2, 2.0), (3, 10, 0.3)],
)
def test_weight_for(ups: int, downs: int, expected: float) -> None:
    assert weight_for(ups, downs) == expected


async def test_update_source_weights_writes_only_changes() -> None:
    stub = StubStore(
        sources=[
            src(1),
            src(2, weight=1.5),
            src(3, enabled=False),
            src(4, weight=1.0, enabled=False),
        ],
        feedback={1: (2, 0), 2: (0, 0), 4: (0, 3)},
    )
    changed = await update_source_weights(cast(Store, stub), NOW)
    assert changed == {1: 1.2, 2: 1.0, 4: 0.7}
    assert stub.weights == changed
    assert stub.feedback_asked == [(FEEDBACK_DAYS, NOW)]
    assert FEEDBACK_DAYS == 30


async def test_set_feedback_rejects_bad_values() -> None:
    stub = StubStore(sources=[src(1)])
    with pytest.raises(ValueError):
        await set_feedback(cast(Store, stub), FixedClock(NOW), 5, 2)
    assert stub.story_feedback == {} and stub.weights == {}


async def test_set_feedback_stores_thumb_and_updates_weights() -> None:
    stub = StubStore(sources=[src(1)], feedback={1: (1, 0)})
    changed = await set_feedback(cast(Store, stub), FixedClock(NOW), 5, 1)
    assert stub.story_feedback == {5: 1}
    assert changed == {1: 1.1}


async def test_add_blocklist_keyword_normalises_and_dedupes() -> None:
    stub = StubStore()
    assert await add_blocklist_keyword(cast(Store, stub), "  Crypto   Airdrop ") == [
        "crypto airdrop"
    ]
    assert await add_blocklist_keyword(cast(Store, stub), "CRYPTO airdrop") == ["crypto airdrop"]
    assert await add_blocklist_keyword(cast(Store, stub), "giveaway") == [
        "crypto airdrop",
        "giveaway",
    ]
    assert stub.settings["blocklist"] == ["crypto airdrop", "giveaway"]


async def test_add_blocklist_keyword_rejects_empty_and_long() -> None:
    stub = StubStore()
    with pytest.raises(ValueError):
        await add_blocklist_keyword(cast(Store, stub), "   ")
    with pytest.raises(ValueError):
        await add_blocklist_keyword(cast(Store, stub), "x" * 61)
    assert "blocklist" not in stub.settings


async def test_thumbs_move_source_weights_in_real_store(store: Store) -> None:
    lab = await add_source(store, "Lab blog", trust=Trust.PRIMARY)
    person = await add_source(store, "Researcher", trust=Trust.TRUSTED)
    i1 = await add_item(store, lab.id, "Lab ships model", "https://example.com/1", NOW)
    i2 = await add_item(store, person.id, "Lab ships model: notes", "https://example.com/2", NOW)
    story = await store.create_story("Lab ships model", Category.MODEL_RELEASE, NOW)
    await store.attach_item(i1, story, NOW)
    await store.attach_item(i2, story, NOW)
    clock = FixedClock(NOW)

    async def weights() -> dict[int, float]:
        return {s.id: s.weight for s in await store.list_sources(enabled_only=False)}

    await set_feedback(store, clock, story, 1)
    assert await weights() == {lab.id: 1.1, person.id: 1.1}
    await set_feedback(store, clock, story, -1)
    assert await weights() == {lab.id: 0.9, person.id: 0.9}
    await set_feedback(store, clock, story, 0)
    assert await weights() == {lab.id: 1.0, person.id: 1.0}
