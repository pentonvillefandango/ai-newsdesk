from __future__ import annotations

from datetime import date, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.web import seed
from tests.web.support import NOW, run


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
