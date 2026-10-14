from typing import Any

from ai_newsdesk.enrich import enrich_bodies
from ai_newsdesk.models import SearchHit, SourceKind
from tests.fakes import FakeResearchEngine

LONG_LINE = (
    "The new model is available in the API today, priced at one dollar per million input tokens."
)
ARTICLE = f"# Launch\n\n{LONG_LINE}\n\nShare\n\n{LONG_LINE}"
EXCERPT = f"{LONG_LINE} {LONG_LINE}"


class TinyStore:
    def __init__(self, rows: list[tuple[int, str, SourceKind, str]]) -> None:
        self.rows = rows
        self.updates: list[tuple[int, dict[str, Any]]] = []
        self.lookups: list[list[int]] = []

    async def items_for_enrichment(
        self, item_ids: list[int]
    ) -> list[tuple[int, str, SourceKind, str]]:
        self.lookups.append(item_ids)
        return [row for row in self.rows if row[0] in item_ids]

    async def set_item_fields(self, item_id: int, **fields: Any) -> None:
        self.updates.append((item_id, fields))


class RecordingEngine:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.fetched: list[str] = []

    async def search(
        self, query: str, *, max_results: int = 10, time_range: str | None = "day"
    ) -> list[SearchHit]:
        return []

    async def fetch_markdown(self, url: str) -> str | None:
        self.fetched.append(url)
        return self.pages.get(url)

    async def health(self) -> bool:
        return True


async def test_thin_items_get_an_excerpt() -> None:
    store = TinyStore([(1, "https://lab.example/a", SourceKind.FEED, "Short.")])
    engine = FakeResearchEngine(pages={"https://lab.example/a": ARTICLE})
    assert await enrich_bodies(store, engine, [1]) == 1
    assert store.updates == [(1, {"body_excerpt": EXCERPT})]


async def test_thick_and_x_items_are_not_fetched() -> None:
    store = TinyStore(
        [
            (1, "https://lab.example/long", SourceKind.FEED, "x" * 300),
            (2, "https://x.com/karpathy/status/1", SourceKind.X_PERSON, "Short."),
            (3, "https://twitter.com/emollick/status/2", SourceKind.SEARCH, "Short."),
            (4, "https://news.example/b", SourceKind.HN, ""),
        ]
    )
    engine = RecordingEngine({"https://news.example/b": ARTICLE})
    assert await enrich_bodies(store, engine, [1, 2, 3, 4]) == 1
    assert engine.fetched == ["https://news.example/b"]
    assert store.updates == [(4, {"body_excerpt": EXCERPT})]


async def test_failed_or_useless_fetch_keeps_the_snippet() -> None:
    snippet = "A snippet that is longer than the fetched page's prose, so it stays."
    store = TinyStore(
        [
            (1, "https://lab.example/missing", SourceKind.FEED, "Short."),
            (2, "https://lab.example/menu-only", SourceKind.SEARCH, snippet),
        ]
    )
    engine = FakeResearchEngine(pages={"https://lab.example/menu-only": "Home\nAbout\nSubscribe"})
    assert await enrich_bodies(store, engine, [1, 2]) == 0
    assert store.updates == []


async def test_custom_thin_threshold() -> None:
    store = TinyStore([(1, "https://lab.example/a", SourceKind.FEED, "y" * 50)])
    engine = RecordingEngine({"https://lab.example/a": ARTICLE})
    assert await enrich_bodies(store, engine, [1], thin_chars=40) == 0
    assert engine.fetched == []


async def test_no_ids_means_no_work() -> None:
    store = TinyStore([])
    assert await enrich_bodies(store, FakeResearchEngine(), []) == 0
    assert store.lookups == []
