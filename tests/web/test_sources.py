from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_newsdesk.models import SourceKind, SourceResult, Trust
from tests.web import seed
from tests.web.support import HX, NOW, csrf_token, run


def _form(client: TestClient, **values: str) -> dict[str, str]:
    return {"csrf_token": csrf_token(client), **values}


def _hx(client: TestClient) -> dict[str, str]:
    return {**HX, "X-CSRF-Token": csrf_token(client)}


def test_sources_page_groups_by_kind_with_health(app: FastAPI, client: TestClient) -> None:
    store = app.state.store
    feed = run(client, seed.add_source, store, name="Zeta Lab")
    run(
        client,
        seed.add_source,
        store,
        name="Ada",
        kind=SourceKind.X_PERSON,
        trust=Trust.TRUSTED,
        config={"handle": "ada_l"},
    )
    run(
        client,
        store.record_source_result,
        SourceResult(source_id=feed, error="HTTP 503 <from> feed"),
        NOW,
    )
    r = client.get("/sources")
    assert r.status_code == 200
    text = r.text
    assert text.index(">Feeds</h2>") < text.index(">People on X</h2>")
    assert "Zeta Lab" in text
    assert "@ada_l" in text
    assert "Last error: HTTP 503 &lt;from&gt; feed" in text
    assert "<dt>Items this week</dt>" in text
    assert 'aria-current="page">Sources' in text


def test_add_a_feed(app: FastAPI, client: TestClient) -> None:
    r = client.post(
        "/sources",
        data=_form(
            client,
            kind="feed",
            name="Example Blog",
            trust="trusted",
            value="https://blog.example/feed.xml",
        ),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/sources?added=")
    sources = run(client, app.state.store.list_sources, enabled_only=False)
    [added] = [s for s in sources if s.name == "Example Blog"]
    assert added.kind == SourceKind.FEED
    assert added.trust == Trust.TRUSTED
    assert added.config == {"url": "https://blog.example/feed.xml"}
    page = client.get(r.headers["location"])
    assert "Source added." in page.text


def test_add_rejects_bad_values_and_keeps_the_form(client: TestClient) -> None:
    r = client.post(
        "/sources",
        data=_form(
            client, kind="feed", name="Blog", trust="trusted", value="ftp://blog.example/feed"
        ),
    )
    assert r.status_code == 422
    assert "starts with http:// or https://" in r.text
    assert 'value="ftp://blog.example/feed"' in r.text
    assert '<details class="add-source" open>' in r.text
    r = client.post(
        "/sources",
        data=_form(client, kind="x_person", name="A", trust="trusted", value="not a handle!"),
    )
    assert r.status_code == 422
    assert "1 to 15 letters, digits or underscores" in r.text
    r = client.post(
        "/sources", data=_form(client, kind="hn", name="HN", trust="trusted", value="x")
    )
    assert r.status_code == 422
    assert "Choose what kind of source this is." in r.text
    r = client.post(
        "/sources",
        data=_form(client, kind="feed", name="", trust="trusted", value="https://a.example/f"),
    )
    assert r.status_code == 422
    assert "name of 1 to 80 characters" in r.text


def test_add_an_x_person_strips_the_at(app: FastAPI, client: TestClient) -> None:
    client.post(
        "/sources",
        data=_form(client, kind="x_person", name="Andrej", trust="trusted", value="@karpathy"),
    )
    sources = run(client, app.state.store.list_sources, enabled_only=False)
    [added] = [s for s in sources if s.name == "Andrej"]
    assert added.config == {"handle": "karpathy"}


def test_edit_inline_with_htmx(app: FastAPI, client: TestClient) -> None:
    store = app.state.store
    src = run(client, seed.add_source, store, name="Old name")
    form = client.get(f"/sources/{src}/edit", headers=HX)
    assert form.status_code == 200
    assert form.text.lstrip().startswith('<li class="source source--editing"')
    assert 'value="Old name"' in form.text
    r = client.post(
        f"/sources/{src}",
        data={"name": "New name", "trust": "discovery", "value": "https://new.example/feed.xml"},
        headers=_hx(client),
    )
    assert r.status_code == 200
    assert r.text.lstrip().startswith(f'<li class="source" id="source-{src}"')
    assert "New name" in r.text and "Discovery" in r.text
    stored = run(client, store.get_source, src)
    assert stored is not None
    assert stored.name == "New name"
    assert stored.trust == Trust.DISCOVERY
    assert stored.config == {"url": "https://new.example/feed.xml"}
    bad = client.post(
        f"/sources/{src}",
        data={"name": "New name", "trust": "discovery", "value": "nope"},
        headers=_hx(client),
    )
    assert bad.status_code == 422
    assert "source--editing" in bad.text
    assert "starts with http:// or https://" in bad.text
    row = client.get(f"/sources/{src}/row", headers=HX)
    assert "New name" in row.text and "source--editing" not in row.text


def test_edit_without_htmx_renders_the_full_page(app: FastAPI, client: TestClient) -> None:
    src = run(client, seed.add_source, app.state.store, name="Plain")
    r = client.get(f"/sources/{src}/edit")
    assert r.status_code == 200
    assert "<html" in r.text
    assert f'<li class="source source--editing" id="source-{src}">' in r.text
    r = client.post(
        f"/sources/{src}",
        data=_form(
            client, name="Plain renamed", trust="primary", value="https://plain.example/feed"
        ),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/sources#source-{src}"


def test_edit_and_mute_keep_the_learned_weight(app: FastAPI, client: TestClient) -> None:
    store = app.state.store
    src = run(client, seed.add_source, store, name="Weighted")
    run(client, store.set_source_weight, src, 1.5)
    client.post(
        f"/sources/{src}",
        data={"name": "Weighted, renamed", "trust": "trusted", "value": "https://w.example/feed"},
        headers=_hx(client),
    )
    client.post(f"/sources/{src}/mute", headers=_hx(client))
    stored = run(client, store.get_source, src)
    assert stored is not None
    assert (stored.name, stored.enabled, stored.weight) == ("Weighted, renamed", False, 1.5)


def test_mute_toggles(app: FastAPI, client: TestClient) -> None:
    store = app.state.store
    src = run(client, seed.add_source, store, name="Noisy")
    r = client.post(f"/sources/{src}/mute", headers=_hx(client))
    assert r.status_code == 200
    assert ">Unmute</button>" in r.text and "source--muted" in r.text
    stored = run(client, store.get_source, src)
    assert stored is not None and stored.enabled is False
    r = client.post(f"/sources/{src}/mute", data=_form(client), follow_redirects=False)
    assert r.status_code == 303
    stored = run(client, store.get_source, src)
    assert stored is not None and stored.enabled is True


def test_delete(app: FastAPI, client: TestClient) -> None:
    store = app.state.store
    src = run(client, seed.add_source, store, name="Gone soon")
    r = client.post(f"/sources/{src}/delete", headers=_hx(client))
    assert r.status_code == 200
    assert r.text == ""
    assert run(client, store.get_source, src) is None
    other = run(client, seed.add_source, store, name="Gone too")
    r = client.post(f"/sources/{other}/delete", data=_form(client), follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/sources"


def test_unknown_source_is_404(client: TestClient) -> None:
    assert client.get("/sources/999999/edit").status_code == 404
    assert client.get("/sources/999999/row", headers=HX).status_code == 404
    assert client.post("/sources/999999/mute", headers=_hx(client)).status_code == 404
    assert client.post("/sources/999999/delete", headers=_hx(client)).status_code == 404
