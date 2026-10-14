from __future__ import annotations

import hashlib
from html.parser import HTMLParser

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_newsdesk.web.deps import STATIC_DIR, templates
from tests.web.support import api_routes, csrf_token

HTMX_SHA256 = "d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717"
ICONS = ("thumbs-up", "thumbs-down", "external-link", "refresh", "x-logo", "rss", "hn")


class _Assets(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[str | None] = []
        self.inline_script = False
        self.stylesheets: list[str] = []
        self.style_elements = 0
        self.style_attrs = 0
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if "style" in a:
            self.style_attrs += 1
        if tag == "script":
            self.scripts.append(a.get("src"))
            self._in_script = True
        elif tag == "style":
            self.style_elements += 1
        elif tag == "link" and a.get("rel") == "stylesheet":
            self.stylesheets.append(a.get("href") or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script and data.strip():
            self.inline_script = True


def _check_page(html: str, where: str) -> None:
    p = _Assets()
    p.feed(html)
    assert p.scripts == ["/static/htmx.min.js"], where
    assert not p.inline_script, where
    assert p.style_elements == 0, where
    assert p.style_attrs == 0, where
    assert p.stylesheets and all(href.startswith("/static/") for href in p.stylesheets), where


def test_vendored_htmx_matches_the_pin() -> None:
    assert hashlib.sha256((STATIC_DIR / "htmx.min.js").read_bytes()).hexdigest() == HTMX_SHA256


def test_icon_sprite_has_every_icon() -> None:
    sprite = (STATIC_DIR / "icons.svg").read_text()
    for name in ICONS:
        assert f'id="i-{name}"' in sprite


def test_stylesheet_covers_both_themes_and_motion() -> None:
    css = (STATIC_DIR / "app.css").read_text()
    for needle in (
        "prefers-color-scheme: dark",
        "prefers-reduced-motion: reduce",
        "forced-colors: active",
        ":focus-visible",
        "--accent:",
        "--font-display:",
        ".gauge",
        ".chip",
        ".meter",
        ".chart__bar",
    ):
        assert needle in css, needle


def test_static_files_are_served_without_login(anon: TestClient) -> None:
    for path, kind in (
        ("/static/app.css", "text/css"),
        ("/static/icons.svg", "image/svg+xml"),
        ("/static/htmx.min.js", "javascript"),
    ):
        r = anon.get(path)
        assert r.status_code == 200, path
        assert kind in r.headers["content-type"], path


def test_base_template_has_masthead_banner_and_only_local_assets() -> None:
    html = templates.get_template("base.html").render(
        csrf_token="tok", active="sources", budget_reached=True
    )
    _check_page(html, "base.html")
    assert '<a href="/sources" aria-current="page">Sources</a>' in html
    assert "budget is spent" in html
    assert 'hx-headers=\'{"X-CSRF-Token": "tok"}\'' in html
    quiet = templates.get_template("base.html").render(
        csrf_token="tok", active="today", budget_reached=False
    )
    assert "budget is spent" not in quiet


def test_every_page_loads_only_local_assets(app: FastAPI, client: TestClient) -> None:
    pages = [
        r.path
        for r in api_routes(app)
        if "GET" in (r.methods or set()) and "{" not in r.path and r.path != "/health"
    ]
    assert "/login" in pages  # Task 28 adds "/" (Today); restore `"/" in pages` there
    for path in pages:
        r = client.get(path)
        if r.headers.get("content-type", "").startswith("text/html"):
            _check_page(r.text, path)
    client.post("/logout", data={"csrf_token": csrf_token(client)})
    login_page = client.get("/login")
    assert login_page.status_code == 200
    _check_page(login_page.text, "/login")
