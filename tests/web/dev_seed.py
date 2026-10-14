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

from ai_newsdesk.models import (
    Candidate,
    Category,
    ItemStatus,
    Purpose,
    SourceKind,
    StoryText,
    Trust,
    Usage,
    Verdict,
)
from ai_newsdesk.store import Store
from ai_newsdesk.store.records import CompareModel, CompareResult
from tests.web import seed

# (handle, hours since published or None, signal or None, post text). Fictional accounts only.
X_POSTS = [
    (
        "example_researcher",
        1,
        8,
        "New paper out today: we trained a 3B model on curated textbooks only and it beats our 7B baseline on maths.\nCode and data cards are linked in the repo.",
    ),
    (
        "example_lab_lead",
        3,
        7,
        "We are opening the evaluation harness we use internally. It includes the failure cases, not just the scores.",
    ),
    (
        "example_researcher",
        5,
        None,
        "Quick thread on why long-context benchmarks disagree so much with each other.",
    ),
    (
        "example_engineer",
        None,
        None,
        "Spent the weekend running the new small model locally. Latency is fine on a laptop GPU; quality is close to the hosted one for code review.",
    ),
    (
        "example_lab_lead",
        26,
        6,
        "Reminder: the office hours on interpretability tooling are on Thursday.",
    ),
]

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


HAIKU = CompareModel(provider="anthropic", model="claude-haiku-5-5")
GEMINI = CompareModel(provider="openrouter", model="google/gemini-3.8-flash")
NANO = CompareModel(provider="openai", model="gpt-5.4-nano")
# Per judged item: (Haiku, Gemini, nano) signals; None = that model failed the item.
JUDGE_SIGNALS = [
    (9, 8, 9),
    (8, 7, 5),
    (6, 3, 7),
    (7, 7, 6),
    (5, 2, 6),
    (6, 6, None),
    (3, 5, 2),
    (8, 8, 8),
    (2, 1, 4),
]
WRITER_TEXTS = {
    GEMINI.key: [
        "Northwind AI released a small model that scores within two points of last year's flagship on reasoning tests. It runs on one consumer GPU, and the weights are under a research licence.",
        "Halcyon Research hid facts at many depths in long documents and found every tested model lost accuracy well before its advertised context limit, often past 200k tokens.",
        "Pinecrest Labs' coding assistant can now index a repository and answer questions about it offline, using a bundled small model.",
    ],
}


async def seed_comparisons(store: Store, now: datetime) -> None:
    """One finished judge comparison (with labels) and one writer comparison (with a
    preference), plus a capped one, so the Compare page has content."""
    sample = await store.compare_judge_inputs(now, 30)
    ids = [s.item_id for s in sample][: len(JUDGE_SIGNALS)]
    models = [HAIKU, GEMINI, NANO]
    run_id = await store.create_compare_run("judge", models, ids, 0.25, now - timedelta(hours=3))
    rows: list[CompareResult] = []
    for item_id, signals in zip(ids, JUDGE_SIGNALS, strict=True):
        for m, signal, latency, cost in zip(
            models, signals, (1400, 2100, 900), (0.0011, 0.0016, 0.0004), strict=True
        ):
            output = (
                Verdict(
                    item_id=item_id, signal=signal, junk=signal < 3, category=Category.RESEARCH
                ).model_dump(mode="json")
                if signal is not None
                else None
            )
            rows.append(
                CompareResult(
                    model_key=m.key,
                    subject_id=item_id,
                    output=output,
                    ok=output is not None,
                    latency_ms=latency,
                    cost_usd=cost,
                )
            )
    await store.add_compare_results(run_id, rows)
    await store.finish_compare_run(
        run_id, "done", now - timedelta(hours=3) + timedelta(minutes=1), 0.0279, None
    )
    for item_id, label in zip(ids, ("important", "show", "junk", "show"), strict=False):
        await store.set_label(item_id, label, now)  # type: ignore[arg-type]

    stories = await store.compare_writer_inputs(now, 3, True)
    story_ids = [w.story_id for w in stories]
    run_id = await store.create_compare_run(
        "writer", [HAIKU, GEMINI], story_ids, 0.25, now - timedelta(hours=2)
    )
    rows = []
    for n, story_id in enumerate(story_ids):
        texts = {
            HAIKU.key: f"Haiku's take on story {n + 1}: what happened, who did it and the key numbers, in two plain sentences.",
            GEMINI.key: WRITER_TEXTS[GEMINI.key][n % 3],
        }
        for m, latency in ((HAIKU, 1900), (GEMINI, 2600)):
            text = StoryText(story_id=story_id, summary=texts[m.key])
            rows.append(
                CompareResult(
                    model_key=m.key,
                    subject_id=story_id,
                    output=text.model_dump(mode="json"),
                    ok=True,
                    latency_ms=latency,
                    cost_usd=0.0009,
                )
            )
    await store.add_compare_results(run_id, rows)
    await store.finish_compare_run(
        run_id, "done", now - timedelta(hours=2) + timedelta(minutes=1), 0.0054, None
    )
    if story_ids:
        await store.set_writer_pref(run_id, story_ids[0], GEMINI.key, now)

    capped = await store.create_compare_run(
        "judge", [GEMINI, NANO], ids[:2], 0.01, now - timedelta(days=1)
    )
    await store.add_compare_results(
        capped,
        [
            CompareResult(model_key=m.key, subject_id=i, ok=False)
            for m in (GEMINI, NANO)
            for i in ids[:2]
        ],
    )
    await store.finish_compare_run(capped, "capped", now - timedelta(days=1), 0.0, None)


async def main(db_path: str) -> None:
    now = datetime.now(UTC)
    store = Store(db_path)
    await store.init()
    try:
        await store.seed_sources_if_empty(Path("config/sources.yaml"))
        lab = await seed.add_source(store, name="Northwind AI blog")
        paper = await seed.add_source(store, name="Halcyon Research", trust=Trust.TRUSTED)
        people: dict[str, int] = {}
        for handle, name in (
            ("example_researcher", "A respected researcher"),
            ("example_lab_lead", "@example_lab_lead"),
            ("example_engineer", "@example_engineer"),
        ):
            people[handle] = await seed.add_source(
                store,
                name=name,
                kind=SourceKind.X_PERSON,
                trust=Trust.TRUSTED,
                config={"handle": handle},
            )
        ids: list[int] = []
        for n, (category, signal, headline, summary, why) in enumerate(STORIES):
            sources = [(lab, f"https://northwind.example/post-{n}", headline)]
            if n < 2:
                sources.append(
                    (paper, f"https://halcyon.example/note-{n}", f"Notes on: {headline}")
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
        # X posts live on their own page (feedback-1), never in Today's stories.
        for n, (handle, hours, signal, text) in enumerate(X_POSTS):
            published = now - timedelta(hours=hours) if hours is not None else None
            [item_id] = await store.add_candidates(
                [
                    Candidate(
                        source_id=people[handle],
                        url=f"https://x.com/{handle}/status/{2000 + n}",
                        title=f"{handle} on X",
                        author=handle,
                        snippet=text,
                        published_at=published,
                    )
                ],
                now - timedelta(minutes=30 + n * 40),
            )
            if signal is not None:
                verdict = Verdict(
                    item_id=item_id, signal=signal, junk=False, category=Category.RESEARCH
                )
                await store.save_verdict(verdict, float(signal))
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
        await seed_comparisons(store, now)
    finally:
        await store.close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].endswith(".sqlite"):
        raise SystemExit("usage: python -m tests.web.dev_seed data/dev.sqlite")
    asyncio.run(main(sys.argv[1]))
