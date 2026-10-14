from __future__ import annotations

from datetime import timedelta
from html.parser import HTMLParser
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_newsdesk.models import Category, ItemStatus, Purpose, Usage
from ai_newsdesk.runtime_settings import update_runtime_settings
from tests.web import seed
from tests.web.support import HX, NOW, csrf_token, run

AT = NOW - timedelta(hours=2)


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            a = dict(attrs)
            self.links.append((a.get("href") or "", a.get("rel") or ""))


def _source(client: TestClient, app: FastAPI, name: str = "Example Lab") -> int:
    return run(client, seed.add_source, app.state.store, name=name)


def _story(client: TestClient, app: FastAPI, src: int, headline: str, url: str, **kw: Any) -> int:
    return run(
        client,
        seed.add_story,
        app.state.store,
        at=AT,
        headline=headline,
        sources=[(src, url, headline)],
        **kw,
    )


def _runtime(client: TestClient, app: FastAPI, **changes: Any) -> None:
    run(client, update_runtime_settings, app.state.store, **changes)


def test_today_shows_the_story_card(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    _story(
        client,
        app,
        src,
        "Example Lab ships a new model",
        "https://lab.example/news/model",
        signal=8,
        summary="The model is faster.",
        why="It changes default choices.",
    )
    r = client.get("/")
    assert r.status_code == 200
    for text in (
        "Wednesday 14 October",
        "Example Lab ships a new model",
        "The model is faster.",
        "Why it matters.",
        "It changes default choices.",
        "Signal 8",
        'data-signal="8"',
        "2 h ago",
        "Example Lab",
        'href="/static/icons.svg#i-rss"',
        'aria-current="page">Today',
    ):
        assert text in r.text, text


def test_why_it_matters_is_hidden_when_switched_off(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    _story(client, app, src, "A story", "https://lab.example/a", why="Hidden reason.")
    _runtime(client, app, why_it_matters=False)
    r = client.get("/")
    assert "A story" in r.text
    assert "Hidden reason." not in r.text
    assert "Why it matters." not in r.text


def test_top_view_orders_by_score_with_a_lead(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    second = _story(client, app, src, "Second best", "https://lab.example/2", signal=6)
    first = _story(client, app, src, "Best of the day", "https://lab.example/1", signal=9)
    r = client.get("/?view=nonsense")
    assert r.text.index("Best of the day") < r.text.index("Second best")
    assert r.text.count("story--lead") == 1
    assert f'class="story story--lead" id="story-{first}"' in r.text
    assert f'class="story" id="story-{second}"' in r.text


def test_section_view_groups_by_category(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    _story(
        client,
        app,
        src,
        "A research result",
        "https://lab.example/r",
        category=Category.RESEARCH,
        signal=9,
    )
    _story(
        client,
        app,
        src,
        "A model release",
        "https://lab.example/m",
        category=Category.MODEL_RELEASE,
        signal=6,
    )
    r = client.get("/?view=category")
    text = r.text
    assert text.index(">Model releases</h2>") < text.index(">Research</h2>")
    assert text.index("A model release") < text.index("A research result")
    assert 'class="index"' in text
    assert 'href="#section-model_release"' in text
    assert "story--lead" not in text


def test_untrusted_text_is_escaped(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    _story(
        client,
        app,
        src,
        "<script>alert(1)</script> Model",
        "https://lab.example/x",
        summary="<img src=x onerror=alert(2)>",
        why="<b>bold</b>",
    )
    r = client.get("/")
    assert "&lt;script&gt;alert(1)&lt;/script&gt; Model" in r.text
    assert "<script>alert(1)" not in r.text
    assert "<img src=x" not in r.text
    assert "<b>bold</b>" not in r.text


def test_outbound_links_get_noopener_noreferrer(app: FastAPI, client: TestClient) -> None:
    lab = _source(client, app, "Lab")
    blog = _source(client, app, "Blog")
    run(
        client,
        seed.add_story,
        app.state.store,
        at=AT,
        headline="Two sources",
        sources=[
            (lab, "https://lab.example/p", "Lab post"),
            (blog, "https://blog.example/p", "Blog post"),
        ],
    )
    parser = _Links()
    parser.feed(client.get("/").text)
    outbound = [(href, rel) for href, rel in parser.links if href.startswith("http")]
    assert len(outbound) >= 5  # headline, 2 chips, 2 links in "All 2 sources"
    for href, rel in outbound:
        assert {"noopener", "noreferrer"} <= set(rel.split()), href


def test_every_source_is_one_click_away(app: FastAPI, client: TestClient) -> None:
    lab = _source(client, app, "Lab")
    blog = _source(client, app, "Blog")
    run(
        client,
        seed.add_story,
        app.state.store,
        at=AT,
        headline="Two sources",
        sources=[
            (lab, "https://lab.example/p", "Lab post"),
            (blog, "https://blog.example/p", "Blog post"),
        ],
    )
    r = client.get("/")
    assert "All 2 sources" in r.text
    assert "Lab post" in r.text and "Blog post" in r.text


def test_thumbs_via_htmx_return_the_updated_card(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    sid = _story(client, app, src, "Thumb me", "https://lab.example/t")
    headers = {**HX, "X-CSRF-Token": csrf_token(client)}
    r = client.post(f"/stories/{sid}/feedback", data={"value": "1", "lead": "1"}, headers=headers)
    assert r.status_code == 200
    assert r.text.lstrip().startswith(f'<article class="story story--lead" id="story-{sid}"')
    assert 'aria-pressed="true"' in r.text
    assert "<html" not in r.text
    assert run(client, app.state.store.stories_by_ids, [sid])[sid].feedback == 1
    r = client.post(f"/stories/{sid}/feedback", data={"value": "0"}, headers=headers)
    assert r.text.lstrip().startswith(f'<article class="story" id="story-{sid}"')
    assert 'aria-pressed="true"' not in r.text
    assert run(client, app.state.store.stories_by_ids, [sid])[sid].feedback == 0


def test_thumbs_without_htmx_redirect_back(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    sid = _story(client, app, src, "Thumb me", "https://lab.example/t")
    r = client.post(
        f"/stories/{sid}/feedback",
        data={"value": "-1", "csrf_token": csrf_token(client)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/#story-{sid}"
    assert run(client, app.state.store.stories_by_ids, [sid])[sid].feedback == -1


def test_thumbs_go_through_step_4_and_move_source_weights(app: FastAPI, client: TestClient) -> None:
    store = app.state.store
    src = _source(client, app)
    sid = _story(client, app, src, "Thumb me", "https://lab.example/t")
    before = run(client, store.get_source, src)
    assert before is not None
    client.post(
        f"/stories/{sid}/feedback",
        data={"value": "1"},
        headers={**HX, "X-CSRF-Token": csrf_token(client)},
    )
    after = run(client, store.get_source, src)
    assert after is not None and after.weight > before.weight


def test_never_show_stories_like_this_adds_a_blocklist_keyword(
    app: FastAPI, client: TestClient
) -> None:
    store = app.state.store
    src = _source(client, app)
    sid = _story(client, app, src, "Crypto token launches an AI agent", "https://lab.example/c")
    page = client.get("/").text
    assert "Never show stories like this" in page
    assert f'action="/stories/{sid}/block"' in page
    r = client.post(
        f"/stories/{sid}/block",
        data={"keyword": "  Crypto   Token "},
        headers={**HX, "X-CSRF-Token": csrf_token(client)},
    )
    assert r.status_code == 200
    assert r.text.lstrip().startswith(f'<article class="story" id="story-{sid}"')
    assert "Added to the blocklist" in r.text
    assert run(client, store.get_setting, "blocklist") == ["crypto token"]
    r = client.post(
        f"/stories/{sid}/block",
        data={"keyword": "agents", "csrf_token": csrf_token(client)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/?blocked=1#story-{sid}"
    assert "Added to the blocklist." in client.get(r.headers["location"]).text
    assert run(client, store.get_setting, "blocklist") == ["crypto token", "agents"]


def test_blocklist_rejects_bad_input(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    sid = _story(client, app, src, "Block me", "https://lab.example/b")
    headers = {**HX, "X-CSRF-Token": csrf_token(client)}
    r = client.post(f"/stories/{sid}/block", data={"keyword": "   "}, headers=headers)
    assert r.status_code == 422
    assert "Enter a short word or phrase" in r.text
    assert (
        client.post(
            f"/stories/{sid}/block", data={"keyword": "x" * 61}, headers=headers
        ).status_code
        == 422
    )
    assert run(client, app.state.store.get_setting, "blocklist") is None
    assert (
        client.post("/stories/999999/block", data={"keyword": "x"}, headers=headers).status_code
        == 404
    )
    assert (
        client.post(f"/stories/{sid}/block", data={"keyword": "x"}, headers=HX).status_code == 403
    )


def test_feedback_rejects_bad_input(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    sid = _story(client, app, src, "Thumb me", "https://lab.example/t")
    headers = {**HX, "X-CSRF-Token": csrf_token(client)}
    assert (
        client.post(f"/stories/{sid}/feedback", data={"value": "5"}, headers=headers).status_code
        == 422
    )
    assert (
        client.post("/stories/999999/feedback", data={"value": "1"}, headers=headers).status_code
        == 404
    )
    assert (
        client.post(f"/stories/{sid}/feedback", data={"value": "1"}, headers=HX).status_code == 403
    )


def test_empty_day(client: TestClient) -> None:
    r = client.get("/")
    assert "Nothing above the bar yet" in r.text
    assert 'href="/settings#run"' in r.text


def test_unscored_items_stay_visible(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    run(
        client,
        seed.add_item,
        app.state.store,
        source_id=src,
        url="https://lab.example/u",
        title="Not reviewed <yet>",
        at=AT,
        status=ItemStatus.REVIEW_FAILED,
    )
    r = client.get("/")
    assert "Waiting for review" in r.text
    assert ">Unscored</span>" in r.text
    assert "Not reviewed &lt;yet&gt;" in r.text
    assert "Nothing above the bar yet" not in r.text


def test_budget_reached_shows_banner_and_marker(app: FastAPI, client: TestClient) -> None:
    store = app.state.store
    _runtime(client, app, monthly_budget_usd=0.01)
    usage = Usage(
        provider="anthropic", model="claude-haiku-5-5", purpose=Purpose.REVIEW, input_tokens=100
    )
    run(client, store.add_spend, usage, 0.02, NOW - timedelta(hours=1))
    src = _source(client, app)
    run(
        client,
        seed.add_item,
        store,
        source_id=src,
        url="https://lab.example/q",
        title="Queued item",
        at=AT,
        status=ItemStatus.QUEUED,
    )
    r = client.get("/")
    assert "budget is spent" in r.text
    assert "Unscored: budget reached" in r.text


def test_non_http_source_urls_render_as_plain_text(app: FastAPI, client: TestClient) -> None:
    js = _source(client, app, "Script Source")
    rel = _source(client, app, "Relative Source")
    run(
        client,
        seed.add_story,
        app.state.store,
        at=AT,
        headline="Odd links",
        sources=[(js, "javascript:alert(1)", "JS post"), (rel, "/relative/path", "Rel post")],
    )
    run(
        client,
        seed.add_item,
        app.state.store,
        source_id=js,
        url="javascript:alert(3)",
        title="Unscored js",
        at=AT,
        status=ItemStatus.REVIEW_FAILED,
    )
    text = client.get("/").text
    parser = _Links()
    parser.feed(text)
    for href, _rel in parser.links:
        assert not href.startswith("javascript:"), href
        assert href != "#"
        assert href != "/relative/path"
    assert "javascript:" not in text
    assert "Script Source" in text and "Relative Source" in text
    assert "Odd links" in text and "JS post" in text and "Unscored js" in text
