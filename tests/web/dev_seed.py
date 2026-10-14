# ruff: noqa: E501  # demo prose
"""Fill a development database with fictional stories, a briefing, spend and runs.

Usage: uv run python -m tests.web.dev_seed data/dev.sqlite
Never point it at the production database.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ai_newsdesk.models import Category, ItemStatus, Purpose, SourceKind, Trust, Usage
from ai_newsdesk.store import Store
from tests.web import seed

STORIES = [
    (
        Category.MODEL_RELEASE,
        9,
        "Northwind AI releases a small model that matches last year's flagship",
        "The new model runs on a single consumer GPU and scores within two points of the larger one on common reasoning tests. Weights are available under a research licence.",
        "Strong local models change what is worth paying an API for.",
    ),
    (
        Category.RESEARCH,
        8,
        "Halcyon Research shows long-context recall drops sharply after 200k tokens",
        "A new benchmark hides facts at many depths in long documents. Every tested model loses accuracy well before its advertised limit.",
        "Context-window numbers on spec sheets overstate what you can rely on.",
    ),
    (
        Category.PRODUCT,
        7,
        "Pinecrest Labs adds offline mode to its coding assistant",
        "The assistant can now index and answer questions about a repository without a network connection, using a bundled small model.",
        None,
    ),
    (
        Category.POLICY,
        6,
        "Regulators publish draft guidance on labelling AI-generated images",
        "The draft asks large platforms to attach provenance metadata and to show a visible label when an image is mostly machine-made.",
        "Labelling rules will shape how image tools are built and shipped.",
    ),
    (
        Category.INDUSTRY,
        5,
        "Two inference start-ups merge to share data-centre capacity",
        "The combined company will run one scheduling layer across both fleets and keep both brands for now.",
        None,
    ),
    (
        Category.RESEARCH,
        6,
        "A simple trick cuts hallucinated citations in summaries by half",
        "Asking the model to quote the supporting sentence before writing each claim reduced invented sources in a controlled study.",
        "Cheap prompt-level fixes still move the needle.",
    ),
    (
        Category.OPINION,
        4,
        "An essay argues evals should be published with their failure cases",
        "The author proposes that every benchmark result ship with a sample of the items the model got wrong.",
        None,
    ),
]


async def main(db_path: str) -> None:
    now = datetime.now(UTC)
    store = Store(db_path)
    await store.init()
    try:
        await store.seed_sources_if_empty(Path("config/sources.yaml"))
        lab = await seed.add_source(store, name="Northwind AI blog")
        paper = await seed.add_source(store, name="Halcyon Research", trust=Trust.TRUSTED)
        person = await seed.add_source(
            store,
            name="A respected researcher",
            kind=SourceKind.X_PERSON,
            trust=Trust.TRUSTED,
            config={"handle": "example_researcher"},
        )
        ids: list[int] = []
        for n, (category, signal, headline, summary, why) in enumerate(STORIES):
            sources = [(lab, f"https://northwind.example/post-{n}", headline)]
            if n < 2:
                sources.append(
                    (paper, f"https://halcyon.example/note-{n}", f"Notes on: {headline}")
                )
                sources.append(
                    (
                        person,
                        f"https://x.com/example_researcher/status/{1000 + n}",
                        "Thread with a summary",
                    )
                )
            ids.append(
                await seed.add_story(
                    store,
                    at=now - timedelta(hours=1 + n * 2),
                    headline=headline,
                    sources=sources,
                    category=category,
                    signal=signal,
                    summary=summary,
                    why=why,
                )
            )
        await seed.add_item(
            store,
            source_id=lab,
            url="https://northwind.example/unreviewed",
            title="An item the judge could not review",
            at=now - timedelta(hours=1),
            status=ItemStatus.REVIEW_FAILED,
        )

        # Step 3's markdown-lite: "## " headings, blank-line paragraphs, [text](story url) links.
        def url(n: int) -> str:
            return f"https://northwind.example/post-{n}"

        body = (
            "Small models and honest benchmarks lead the day.\n\n"
            "## Model releases\n"
            f"Northwind AI [released a small model]({url(0)}) that comes close to last year's flagship. "
            "It runs on one consumer GPU.\n\n"
            "## Research\n"
            f"[Long-context recall falls off]({url(1)}) well before advertised limits, and "
            f"[quoting sources before writing claims]({url(5)}) halves invented citations.\n\n"
            "## Policy\n"
            f"[Draft guidance on labelling AI images]({url(3)}) asks platforms for provenance metadata."
        )
        local_day = now.astimezone(ZoneInfo("Europe/London")).date()
        await store.save_briefing(
            local_day, "claude-haiku-5-5", body, ids[:6], now - timedelta(hours=2)
        )
        usage = Usage(
            provider="anthropic",
            model="claude-haiku-5-5",
            purpose=Purpose.REVIEW,
            input_tokens=1000,
        )
        for day in range(1, now.day + 1):
            at = now.replace(day=day, hour=6, minute=0, second=0, microsecond=0)
            if at <= now:
                await store.add_spend(usage, 0.04 + (day % 5) * 0.012, at)
        for hours_ago, error in (
            (18, None),
            (12, None),
            (6, "research-engine timed out for 2 sources"),
        ):
            run_id = await store.start_run("schedule", now - timedelta(hours=hours_ago))
            await store.finish_run(
                run_id, now - timedelta(hours=hours_ago) + timedelta(minutes=3), {}, 0.05, error
            )
    finally:
        await store.close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].endswith(".sqlite"):
        raise SystemExit("usage: python -m tests.web.dev_seed data/dev.sqlite")
    asyncio.run(main(sys.argv[1]))
