"""The X page: posts from the X accounts, kept apart from Today's news stories."""

from __future__ import annotations

from datetime import datetime, timedelta
from html.parser import HTMLParser

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_newsdesk.models import Candidate, ItemStatus, SourceKind, Trust, XPost
from tests.web import seed
from tests.web.support import NOW, run


def _post(item_id: int, **kw: object) -> XPost:
    values: dict[str, object] = {
        "item_id": item_id,
        "handle": "example_researcher",
        "source_name": "@example_researcher",
        "url": f"https://x.com/example_researcher/status/{item_id}",
        "title": f"Post {item_id}",
        "text": f"Text of post {item_id}.",
        "published_at": NOW - timedelta(hours=item_id),
        "fetched_at": NOW - timedelta(hours=item_id),
        "signal": 7,
        "status": ItemStatus.REVIEWED,
        "junk": False,
    }
    values.update(kw)
    return XPost.model_validate(values)


def _serve(app: FastAPI, monkeypatch: pytest.MonkeyPatch, posts: list[XPost]) -> list[datetime]:
    calls: list[datetime] = []

    async def fake(now: datetime, days: int = 7, limit: int = 200) -> list[XPost]:
        calls.append(now)
        return posts

    monkeypatch.setattr(app.state.store, "x_posts", fake)
    return calls


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.links.append(dict(attrs))


def test_x_page_needs_a_session(anon: TestClient) -> None:
    r = anon.get("/x", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login?next=")


def test_x_page_shows_what_the_store_returns_in_order(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _serve(
        app,
        monkeypatch,
        [
            _post(1, text="Newest post.\nSecond line.", signal=8),
            _post(
                2,
                handle="another_person",
                source_name="Another Person",
                published_at=None,
                fetched_at=NOW - timedelta(hours=3),
                signal=None,
                status=ItemStatus.QUEUED,
                junk=None,
            ),
        ],
    )
    r = client.get("/x")
    assert r.status_code == 200
    assert calls == [NOW]
    text = r.text
    assert 'aria-current="page">X</a>' in text
    assert text.index("Newest post.") < text.index("Text of post 2.")
    assert "<p>Newest post.</p>" in text and "<p>Second line.</p>" in text
    assert "@example_researcher" in text
    assert "@another_person" in text and "Another Person" in text
    assert "1 h ago" in text
    assert "seen 3 h ago" in text
    assert "Signal 8" in text
    assert ">Unscored</span>" in text
    assert 'href="/static/icons.svg#i-x-logo"' in text
    opened = [a for a in (_parse(text)) if a.get("href", "").startswith("https://x.com/")]
    assert len(opened) == 2
    for a in opened:
        assert set((a.get("rel") or "").split()) >= {"noopener", "noreferrer"}
    assert "Open on X" in text


def _parse(html: str) -> list[dict[str, str]]:
    p = _Links()
    p.feed(html)
    return [{k: v or "" for k, v in a.items()} for a in p.links]


def test_post_text_is_escaped(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(app, monkeypatch, [_post(1, text="<script>alert(1)</script> hi", handle="a<b>")])
    text = client.get("/x").text
    assert "<script>alert(1)" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt; hi" in text
    assert "a<b>" not in text


def test_unsafe_post_url_is_plain_text(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(app, monkeypatch, [_post(1, url="javascript:alert('x')")])
    text = client.get("/x").text
    assert "Open on X" not in text
    assert '<span class="post__url">javascript:alert(&#39;x&#39;)</span>' in text
    assert not [
        a for a in _parse(text) if "javascript" in a.get("href", "") or a.get("href") == "#"
    ]


def test_empty_x_page(app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(app, monkeypatch, [])
    assert (
        "No posts from your X accounts in the last 7 days yet — they're collected with each run."
        in client.get("/x").text
    )


def test_x_page_reads_real_posts_from_the_store(app: FastAPI, client: TestClient) -> None:
    store = app.state.store
    src = run(
        client,
        seed.add_source,
        store,
        name="@example_researcher",
        kind=SourceKind.X_PERSON,
        trust=Trust.TRUSTED,
        config={"handle": "example_researcher"},
    )
    candidate = Candidate(
        source_id=src,
        url="https://x.com/example_researcher/status/42",
        title="A post",
        snippet="Stored post text.",
        published_at=NOW - timedelta(hours=2),
    )
    run(client, store.add_candidates, [candidate], NOW - timedelta(hours=1))
    text = client.get("/x").text
    assert "Stored post text." in text
    assert "Stored post text." not in client.get("/").text


def test_x_is_in_the_nav_on_every_page(client: TestClient) -> None:
    for path in ("/", "/briefing", "/x", "/sources", "/settings"):
        text = client.get(path).text
        assert '<a href="/x"' in text, path
        assert (
            text.index('href="/briefing"') < text.index('href="/x"') < text.index('href="/sources"')
        ), path
