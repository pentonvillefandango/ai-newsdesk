from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_newsdesk import pipeline
from ai_newsdesk.runtime_settings import update_runtime_settings
from tests.web import seed
from tests.web.support import NOW, csrf_token, run


def _briefing(client: TestClient, app: FastAPI, day: date, body: str, story_ids: list[int]) -> None:
    run(client, app.state.store.save_briefing, day, "claude-haiku-5-5", body, story_ids, NOW)


def test_latest_briefing_renders_headings_paragraphs_and_story_links(
    app: FastAPI, client: TestClient
) -> None:
    store = app.state.store
    src = run(client, seed.add_source, store, name="Example Lab")
    sid = run(
        client,
        seed.add_story,
        store,
        at=NOW - timedelta(hours=3),
        headline="Example Lab ships a model",
        sources=[(src, "https://lab.example/model", "Model post")],
    )
    body = (
        "## Model releases\n\n"
        "Example Lab [shipped a model](https://lab.example/model). "
        "A [stray link](https://evil.example/x) stays text.\n\n"
        "<b>not bold</b> stays text."
    )
    _briefing(client, app, date(2026, 10, 14), body, [sid])
    r = client.get("/briefing")
    assert r.status_code == 200
    text = r.text
    assert "Wednesday 14 October" in text
    assert "<h2>Model releases</h2>" in text
    assert (
        '<a class="ref" href="https://lab.example/model" rel="noopener noreferrer"'
        ' target="_blank">shipped a model</a>' in text
    )
    assert "evil.example" not in text
    assert "A stray link stays text." in text
    assert "](" not in text
    assert "&lt;b&gt;not bold&lt;/b&gt; stays text." in text
    assert "<b>not bold" not in text
    assert "claude-haiku-5-5" in text
    assert 'aria-current="page">Briefing' in text


def test_archive_lists_dates_and_opens_older_briefings(app: FastAPI, client: TestClient) -> None:
    _briefing(client, app, date(2026, 10, 13), "Tuesday news.", [])
    _briefing(client, app, date(2026, 10, 14), "Wednesday news.", [])
    latest = client.get("/briefing").text
    assert "Wednesday news." in latest
    assert 'href="/briefing/2026-10-13">Tue 13 Oct</a>' in latest
    assert 'href="/briefing/2026-10-14" aria-current="page">Wed 14 Oct</a>' in latest
    older = client.get("/briefing/2026-10-13")
    assert older.status_code == 200
    assert "Tuesday news." in older.text
    assert "Wednesday news." not in older.text
    assert 'href="/briefing/2026-10-13" aria-current="page"' in older.text


def test_unknown_impossible_or_malformed_dates_are_404(client: TestClient) -> None:
    for path in (
        "/briefing/2026-10-01",
        "/briefing/2026-02-30",
        "/briefing/latest",
        "/briefing/20261014",
    ):
        r = client.get(path)
        assert r.status_code == 404, path
        assert "No briefing for that day" in r.text


def test_no_briefing_yet(client: TestClient) -> None:
    r = client.get("/briefing")
    assert r.status_code == 200
    assert "No briefing yet" in r.text
    assert "07:00" in r.text


def test_hostile_link_forms_render_no_anchor_to_non_story_urls(
    app: FastAPI, client: TestClient
) -> None:
    store = app.state.store
    src = run(client, seed.add_source, store, name="Example Lab")
    sid = run(
        client,
        seed.add_story,
        store,
        at=NOW - timedelta(hours=3),
        headline="Example Lab ships a model",
        sources=[(src, "https://lab.example/model", "Model post")],
    )
    evil = "https://evil.example"
    forms = [
        f"[]({evil})",
        f'[t]({evil} "x")',
        f"[t]( {evil})",
        f"[a\nb]({evil})",
        f"[a\\]]({evil})",
        f"[t][1]\n\n[1]: {evil}",
        evil,
        f"<{evil}>",
    ]
    body = "\n\n".join(forms) + "\n\n## Head [h](" + evil + ")"
    _briefing(client, app, date(2026, 10, 14), body, [sid])
    r = client.get("/briefing")
    assert r.status_code == 200
    assert "<a " not in r.text.split('class="read"')[1].split("</article>")[0]
    assert 'href="https://evil.example' not in r.text
    assert "<https://evil" not in r.text


def test_story_link_with_non_http_scheme_is_text(app: FastAPI, client: TestClient) -> None:
    store = app.state.store
    src = run(client, seed.add_source, store, name="Example Lab")
    sid = run(
        client,
        seed.add_story,
        store,
        at=NOW - timedelta(hours=3),
        headline="Odd",
        sources=[(src, "javascript:alert(1)", "Odd")],
    )
    _briefing(client, app, date(2026, 10, 14), "A [bad](javascript:alert(1)) link.", [sid])
    text = client.get("/briefing").text
    assert 'href="javascript' not in text
    assert '<a class="ref"' not in text


def test_all_outbound_links_have_noopener(app: FastAPI, client: TestClient) -> None:
    store = app.state.store
    src = run(client, seed.add_source, store, name="Example Lab")
    sid = run(
        client,
        seed.add_story,
        store,
        at=NOW - timedelta(hours=3),
        headline="X",
        sources=[(src, "https://lab.example/model", "X")],
    )
    _briefing(client, app, date(2026, 10, 14), "[a](https://lab.example/model)", [sid])
    text = client.get("/briefing").text
    assert 'rel="noopener noreferrer" target="_blank">a</a>' in text


def test_briefing_skips_story_ids_that_no_longer_exist(app: FastAPI, client: TestClient) -> None:
    """Feedback-1 merges duplicate stories, so an old briefing can name a story id that is gone."""
    store = app.state.store
    src = run(client, seed.add_source, store, name="Example Lab")
    sid = run(
        client,
        seed.add_story,
        store,
        at=NOW - timedelta(hours=3),
        headline="A story that survived",
        sources=[(src, "https://lab.example/kept", "Kept post")],
    )
    body = (
        "[Kept story](https://lab.example/kept) and "
        "[a merged one](https://lab.example/merged-away) are both named."
    )
    _briefing(client, app, date(2026, 10, 14), body, [sid, sid + 999])
    for path in ("/briefing", "/briefing/2026-10-14"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert 'href="https://lab.example/kept"' in r.text
        assert "merged-away" not in r.text
        assert "a merged one" in r.text


def test_the_side_column_has_the_covered_stories_and_the_archive(
    app: FastAPI, client: TestClient
) -> None:
    """Feedback-2: compact cards for the stories the briefing covers, in its order, skipping
    ids that no longer exist; the archive sits in the same column."""
    store = app.state.store
    lab = run(client, seed.add_source, store, name="Example Lab")
    hn = run(client, seed.add_source, store, name="HN")
    first = run(
        client,
        seed.add_story,
        store,
        at=NOW - timedelta(hours=3),
        headline="Example Lab ships a model",
        signal=8,
        sources=[
            (lab, "https://lab.example/model", "Model post"),
            (hn, "https://news.example/item?id=1", "Discussion"),
        ],
    )
    second = run(
        client,
        seed.add_story,
        store,
        at=NOW - timedelta(hours=2),
        headline="A <b>bold</b> claim",
        signal=5,
        sources=[(lab, "javascript:alert(1)", "Bad link")],
    )
    _briefing(client, app, date(2026, 10, 13), "Older.", [])
    _briefing(client, app, date(2026, 10, 14), "Today's news.", [second, 424242, first])
    text = client.get("/briefing").text
    side = text[text.index('<aside class="briefing__side"') : text.index("</aside>")]
    assert '<h2 id="covered-title">In this briefing</h2>' in side
    cards = side.split('<li class="story story--compact"')[1:]
    assert len(cards) == 2  # 424242 was merged away
    assert 'data-signal="5"' in cards[0] and "A &lt;b&gt;bold&lt;/b&gt; claim" in cards[0]
    assert "<a " not in cards[0].split("</h3>")[0]  # an unsafe link renders as text
    assert "1 source<" in cards[0]
    assert 'data-signal="8"' in cards[1]
    assert (
        '<a href="https://lab.example/model" rel="noopener noreferrer" target="_blank">'
        "Example Lab ships a model</a>" in cards[1]
    )
    assert "2 sources<" in cards[1]
    assert '<span class="gauge" aria-hidden="true"></span>' in cards[1]
    # The archive moved into the column.
    assert '<nav class="archive" aria-labelledby="archive-title">' in side
    assert 'href="/briefing/2026-10-13">Tue 13 Oct</a>' in side


def test_a_briefing_without_stories_has_no_covered_list(app: FastAPI, client: TestClient) -> None:
    _briefing(client, app, date(2026, 10, 14), "Quiet day.", [424242])
    text = client.get("/briefing").text
    assert "In this briefing" not in text
    assert '<nav class="archive"' in text


NOTICE = "Today's briefing isn't ready."


def test_notice_shows_on_latest_when_today_is_missing(app: FastAPI, client: TestClient) -> None:
    _briefing(client, app, date(2026, 10, 13), "Tuesday news.", [])
    text = client.get("/briefing").text  # NOW is 09:30 BST on 14 October, after 07:00
    assert NOTICE in text
    assert "Showing the one from Tuesday 13 October." in text
    assert 'action="/briefing/write"' in text
    assert "Write it now" in text
    assert "Tried" not in text  # no scheduler attempts yet
    assert NOTICE not in client.get("/briefing/2026-10-13").text


def test_notice_leaves_out_showing_when_there_is_no_briefing(client: TestClient) -> None:
    text = client.get("/briefing").text
    assert NOTICE in text
    assert "Showing the one from" not in text


def test_no_notice_before_briefing_time_or_when_today_exists(
    app: FastAPI, client: TestClient
) -> None:
    run(client, update_runtime_settings, app.state.store, briefing_time="23:00")
    assert NOTICE not in client.get("/briefing").text
    run(client, update_runtime_settings, app.state.store, briefing_time="07:00")
    assert NOTICE in client.get("/briefing").text
    _briefing(client, app, date(2026, 10, 14), "Today.", [])
    assert NOTICE not in client.get("/briefing").text


def test_notice_wording_for_tries_and_giving_up(app: FastAPI, client: TestClient) -> None:
    sched = app.state.scheduler
    sched._briefing_try_day, sched._briefing_tries = date(2026, 10, 14), 2
    text = client.get("/briefing").text
    assert "Tried 2 of 6 times; the last try failed." in text
    assert "stopped trying" not in text
    sched._briefing_tries = 6
    text = client.get("/briefing").text
    assert "The scheduler has stopped trying today." in text
    assert "Tried 6" not in text
    assert sched._briefing_tries == 6  # reading the page changes nothing


def test_write_now_starts_a_background_briefing(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[date] = []

    async def fake_run_briefing(ctx: object, day: date) -> bool:
        calls.append(day)
        return True

    monkeypatch.setattr(pipeline, "run_briefing", fake_run_briefing)
    r = client.post(
        "/briefing/write", data={"csrf_token": csrf_token(client)}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/briefing?writing=1"

    async def drain() -> None:
        await asyncio.gather(*app.state.background)

    run(client, drain)
    assert calls == [date(2026, 10, 14)]
    assert "Writing today's briefing. This takes about a minute; reload to see it." in (
        client.get(r.headers["location"]).text
    )


@pytest.mark.parametrize("written", [True, False])
def test_write_now_narrates_after_a_written_briefing(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch, written: bool
) -> None:
    lock: asyncio.Lock = app.state.scheduler.lock
    events: list[tuple[str, date, bool]] = []

    async def fake_run_briefing(ctx: object, day: date) -> bool:
        events.append(("brief", day, lock.locked()))
        return written

    async def fake_run_narration(ctx: object, day: date) -> pipeline.NarrationOutcome:
        events.append(("narrate", day, lock.locked()))
        return pipeline.NarrationOutcome.DONE

    monkeypatch.setattr(pipeline, "run_briefing", fake_run_briefing)
    monkeypatch.setattr(pipeline, "run_narration", fake_run_narration)
    client.post("/briefing/write", data={"csrf_token": csrf_token(client)}, follow_redirects=False)

    async def drain() -> None:
        await asyncio.gather(*app.state.background)

    run(client, drain)
    today = date(2026, 10, 14)
    expected = [("brief", today, True)] + ([("narrate", today, True)] if written else [])
    assert events == expected
    assert not lock.locked()


def test_write_now_swallows_a_narration_failure(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_run_briefing(ctx: object, day: date) -> bool:
        return True

    async def boom(ctx: object, day: date) -> pipeline.NarrationOutcome:
        raise RuntimeError("tts down")

    monkeypatch.setattr(pipeline, "run_briefing", fake_run_briefing)
    monkeypatch.setattr(pipeline, "run_narration", boom)
    client.post("/briefing/write", data={"csrf_token": csrf_token(client)}, follow_redirects=False)

    async def drain() -> None:
        await asyncio.gather(*app.state.background)

    run(client, drain)
    assert not app.state.scheduler.lock.locked()


def test_write_now_swallows_a_failure(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(ctx: object, day: date) -> bool:
        raise RuntimeError("writer down")

    monkeypatch.setattr(pipeline, "run_briefing", boom)
    client.post("/briefing/write", data={"csrf_token": csrf_token(client)}, follow_redirects=False)

    async def drain() -> None:
        await asyncio.gather(*app.state.background)

    run(client, drain)
    assert not app.state.scheduler.lock.locked()


def test_write_now_is_busy_while_the_lock_is_held(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[date] = []

    async def fake_run_briefing(ctx: object, day: date) -> bool:
        calls.append(day)
        return True

    monkeypatch.setattr(pipeline, "run_briefing", fake_run_briefing)
    lock: asyncio.Lock = app.state.scheduler.lock
    run(client, lock.acquire)
    try:
        r = client.post(
            "/briefing/write", data={"csrf_token": csrf_token(client)}, follow_redirects=False
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/briefing?writing=busy"
        assert "A run is in progress; try again in a few minutes." in (
            client.get(r.headers["location"]).text
        )
    finally:
        lock.release()
    assert calls == []
    assert len(app.state.background) == 0


def test_write_now_needs_csrf(client: TestClient) -> None:
    assert client.post("/briefing/write").status_code == 403
