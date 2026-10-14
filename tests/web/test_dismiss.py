"""Dismissing cards on Today and on the X page (feedback-1 dismiss)."""

from __future__ import annotations

import re
from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_newsdesk.models import Candidate, Category, SourceKind, Trust
from tests.web import seed
from tests.web.support import HX, NOW, api_routes, csrf_token, run

AT = NOW - timedelta(hours=2)


def _hx(client: TestClient) -> dict[str, str]:
    return {**HX, "X-CSRF-Token": csrf_token(client)}


def _source(client: TestClient, app: FastAPI, name: str = "Example Lab") -> int:
    return run(client, seed.add_source, app.state.store, name=name)


def _story(client: TestClient, app: FastAPI, src: int, headline: str, **kw: object) -> int:
    slug = "-".join(headline.lower().split())
    return run(
        client,
        seed.add_story,
        app.state.store,
        at=AT,
        headline=headline,
        sources=[(src, f"https://lab.example/{slug}", headline)],
        **kw,
    )


def _x_post(client: TestClient, app: FastAPI, n: int, handle: str = "example_researcher") -> int:
    store = app.state.store
    src = run(
        client,
        seed.add_source,
        store,
        name=f"@{handle}",
        kind=SourceKind.X_PERSON,
        trust=Trust.TRUSTED,
        config={"handle": handle},
    )
    candidate = Candidate(
        source_id=src,
        url=f"https://x.com/{handle}/status/{n}",
        title=f"Post {n}",
        snippet=f"Post text {n}.",
        published_at=AT,
    )
    return run(client, store.add_candidates, [candidate], AT)[0]


# ---- buttons ----------------------------------------------------------------------------------


def test_every_story_card_has_a_dismiss_button(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    lead = _story(client, app, src, "Lead story", signal=9)
    card = _story(client, app, src, "Grid & story", signal=7, category=Category.POLICY)
    for path in ("/", "/?view=category"):
        text = client.get(path).text
        for sid in (lead, card):
            assert f'action="/stories/{sid}/dismiss"' in text, path
            assert f'hx-post="/stories/{sid}/dismiss"' in text, path
        assert 'aria-label="Dismiss: Lead story"' in text, path
        assert 'aria-label="Dismiss: Grid &amp; story"' in text, path
    category_page = client.get("/?view=category").text
    assert '<input type="hidden" name="view" value="category">' in category_page


def test_every_x_post_has_a_dismiss_button(app: FastAPI, client: TestClient) -> None:
    post = _x_post(client, app, 1)
    text = client.get("/x").text
    assert f'action="/x/{post}/dismiss"' in text
    assert f'hx-post="/x/{post}/dismiss"' in text
    assert 'aria-label="Dismiss: @example_researcher"' in text


# ---- policy -----------------------------------------------------------------------------------


def test_route_policy_covers_the_new_routes(app: FastAPI) -> None:
    posts = {r.path for r in api_routes(app) if r.methods and "POST" in r.methods}
    assert {
        "/stories/{story_id}/dismiss",
        "/stories/{story_id}/undismiss",
        "/x/{item_id}/dismiss",
        "/x/{item_id}/undismiss",
    } <= posts


def test_dismiss_needs_csrf_and_a_session(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    sid = _story(client, app, src, "A story")
    post = _x_post(client, app, 1)
    for path in (f"/stories/{sid}/dismiss", f"/x/{post}/dismiss"):
        assert client.post(path, headers=HX).status_code == 403
    assert client.get("/").text.count('aria-label="Dismiss:') == 1
    # test_route_policy.py walks every POST route, so these get the 401/403 checks too.


# ---- Today ------------------------------------------------------------------------------------


def test_htmx_dismiss_returns_a_placeholder_with_undo(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    sid = _story(client, app, src, "Dismiss me")
    r = client.post(f"/stories/{sid}/dismiss", data={"lead": "1"}, headers=_hx(client))
    assert r.status_code == 200
    text = r.text
    assert "<html" not in text
    assert f'id="story-{sid}"' in text
    assert "Dismissed" in text
    assert f'hx-post="/stories/{sid}/undismiss"' in text
    assert f'action="/stories/{sid}/undismiss"' in text
    assert ">Undo</button>" in text
    assert 'aria-label="Undo dismiss: Dismiss me"' in text
    assert '<input type="hidden" name="lead" value="1">' in text
    assert 'hx-trigger="load delay:8s"' in text and 'hx-swap="delete"' in text
    assert "<script" not in text and "hx-on" not in text
    assert "Dismiss me" not in client.get("/").text


def test_undo_restores_the_card(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    sid = _story(client, app, src, "Bring me back")
    client.post(f"/stories/{sid}/dismiss", headers=_hx(client))
    r = client.post(f"/stories/{sid}/undismiss", data={"lead": "1"}, headers=_hx(client))
    assert r.status_code == 200
    assert r.text.lstrip().startswith(f'<article class="story story--lead" id="story-{sid}"')
    assert 'aria-label="Dismiss: Bring me back"' in r.text
    assert "Bring me back" in client.get("/").text


def test_placeholder_expiry_endpoint_is_empty(client: TestClient) -> None:
    r = client.get("/dismissed/expire", headers=HX)
    assert r.status_code == 200
    assert r.text == ""


def test_no_js_dismiss_redirects_back_preserving_the_view(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    one = _story(client, app, src, "First story")
    two = _story(client, app, src, "Second story")
    token = csrf_token(client)
    r = client.post(f"/stories/{one}/dismiss", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    r = client.post(
        f"/stories/{two}/dismiss",
        data={"csrf_token": token, "view": "category"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/?view=category"
    r = client.post(
        f"/stories/{two}/dismiss",
        data={"csrf_token": token, "view": "https://evil.example"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"
    page = client.get("/").text
    assert "First story" not in page and "Second story" not in page
    r = client.post(
        f"/stories/{one}/undismiss",
        data={"csrf_token": token, "view": "category", "dismissed": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/?view=category&dismissed=1"
    assert "First story" in client.get("/").text


def test_unknown_ids_are_404(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    lab_item = run(
        client,
        seed.add_item,
        app.state.store,
        source_id=src,
        url="https://lab.example/item",
        title="Not an X post",
        at=AT,
    )
    headers = _hx(client)
    for path in (
        "/stories/999999/dismiss",
        "/stories/999999/undismiss",
        "/x/999999/dismiss",
        "/x/999999/undismiss",
        f"/x/{lab_item}/dismiss",
    ):
        assert client.post(path, headers=headers).status_code == 404, path


def test_dismiss_is_idempotent(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    sid = _story(client, app, src, "Twice")
    for _ in range(2):
        assert client.post(f"/stories/{sid}/dismiss", headers=_hx(client)).status_code == 200
    assert "Show dismissed (1)" in client.get("/").text


def test_lede_counts_only_visible_stories(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    _story(client, app, src, "Stays")
    gone = _story(client, app, src, "Goes")
    client.post(f"/stories/{gone}/dismiss", headers=_hx(client))
    text = client.get("/").text
    assert "1 story worth your time" in text
    assert "Show dismissed (1)" in text
    assert 'href="/?dismissed=1"' in text
    assert 'href="/?view=category&amp;dismissed=1"' in client.get("/?view=category").text


def test_no_show_dismissed_link_when_nothing_is_dismissed(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    _story(client, app, src, "Stays")
    assert "Show dismissed" not in client.get("/").text
    _x_post(client, app, 1)
    assert "Show dismissed" not in client.get("/x").text


def test_dismissed_view_lists_only_dismissed_stories_with_restore(
    app: FastAPI, client: TestClient
) -> None:
    src = _source(client, app)
    _story(client, app, src, "Visible story")
    gone = _story(client, app, src, "Hidden story")
    client.post(f"/stories/{gone}/dismiss", headers=_hx(client))
    text = client.get("/?dismissed=1").text
    assert re.search(r"<h1[^>]*>Dismissed</h1>", text)
    assert "Hidden story" in text and "Visible story" not in text
    assert f'action="/stories/{gone}/undismiss"' in text
    assert 'aria-label="Restore: Hidden story"' in text
    assert 'aria-label="Dismiss: Hidden story"' not in text
    assert '<input type="hidden" name="dismissed" value="1">' in text
    assert 'href="/">Back to Today' in text
    assert "Show dismissed" not in text


def test_dismiss_leaves_feedback_and_source_weights_alone(app: FastAPI, client: TestClient) -> None:
    store = app.state.store
    src = _source(client, app)
    sid = _story(client, app, src, "Rated story")
    client.post(f"/stories/{sid}/feedback", data={"value": "1"}, headers=_hx(client))
    weight = run(client, store.get_source, src).weight
    blocklist = run(client, store.get_setting, "blocklist")
    client.post(f"/stories/{sid}/dismiss", headers=_hx(client))
    client.post(f"/stories/{sid}/undismiss", headers=_hx(client))
    client.post(f"/stories/{sid}/dismiss", headers=_hx(client))
    assert run(client, store.get_source, src).weight == weight
    assert run(client, store.stories_by_ids, [sid])[sid].feedback == 1
    assert run(client, store.get_setting, "blocklist") == blocklist


def test_briefing_still_shows_a_dismissed_story(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    sid = _story(client, app, src, "In the briefing")
    body = "Lab news: [in the briefing](https://lab.example/in-the-briefing)."
    run(client, app.state.store.save_briefing, NOW.date(), "m", body, [sid], NOW)
    client.post(f"/stories/{sid}/dismiss", headers=_hx(client))
    assert 'href="https://lab.example/in-the-briefing"' in client.get("/briefing").text


# ---- X page -----------------------------------------------------------------------------------


def test_x_dismiss_undo_and_dismissed_view(app: FastAPI, client: TestClient) -> None:
    keep = _x_post(client, app, 1)
    gone = _x_post(client, app, 2, handle="another_person")
    r = client.post(f"/x/{gone}/dismiss", headers=_hx(client))
    assert r.status_code == 200
    assert f'id="post-{gone}"' in r.text
    assert f'hx-post="/x/{gone}/undismiss"' in r.text
    assert 'aria-label="Undo dismiss: @another_person"' in r.text
    page = client.get("/x").text
    assert "Post text 2." not in page and "Post text 1." in page
    assert "1 post from the people" in page
    assert "Show dismissed (1)" in page and 'href="/x?dismissed=1"' in page

    view = client.get("/x?dismissed=1").text
    assert re.search(r"<h1[^>]*>Dismissed</h1>", view)
    assert "Post text 2." in view and "Post text 1." not in view
    assert 'aria-label="Restore: @another_person"' in view
    assert 'href="/x">Back to X' in view

    r = client.post(f"/x/{gone}/undismiss", headers=_hx(client))
    assert r.status_code == 200
    assert r.text.lstrip().startswith(f'<article class="post" id="post-{gone}"')
    assert "Post text 2." in client.get("/x").text
    assert keep


def test_x_no_js_dismiss_redirects(app: FastAPI, client: TestClient) -> None:
    post = _x_post(client, app, 1)
    token = csrf_token(client)
    r = client.post(f"/x/{post}/dismiss", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/x"
    r = client.post(
        f"/x/{post}/undismiss",
        data={"csrf_token": token, "dismissed": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/x?dismissed=1"
