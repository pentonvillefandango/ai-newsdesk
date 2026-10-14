from __future__ import annotations

import hashlib
import re
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
    assert p.scripts == ["/static/theme.js", "/static/htmx.min.js"], where
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
        ("/static/theme.js", "javascript"),
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
    assert "/" in pages
    assert "/login" in pages
    for path in pages:
        r = client.get(path)
        if r.headers.get("content-type", "").startswith("text/html"):
            _check_page(r.text, path)
    client.post("/logout", data={"csrf_token": csrf_token(client)})
    login_page = client.get("/login")
    assert login_page.status_code == 200
    _check_page(login_page.text, "/login")


def _luminance(hex_colour: str) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4

    h = hex_colour.lstrip("#")
    r, g, b = (channel(int(h[i : i + 2], 16)) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_light_accent_meets_wcag_aa_for_small_text() -> None:
    css = (STATIC_DIR / "app.css").read_text()
    light = css.split("@media (prefers-color-scheme: dark)")[0]
    tokens = dict(re.findall(r"--(accent|paper|surface):\s*(#[0-9a-fA-F]{6});", light))
    assert set(tokens) == {"accent", "paper", "surface"}
    for bg in ("paper", "surface"):
        assert _contrast(tokens["accent"], tokens[bg]) >= 4.5, bg


def _rule(css: str, selector: str) -> str:
    match = re.search(rf"(?:^|\n){re.escape(selector)}\s*\{{([^}}]*)\}}", css)
    assert match, selector
    return match.group(1)


def test_briefing_links_inherit_the_reading_font() -> None:
    css = (STATIC_DIR / "app.css").read_text()
    body = _rule(css, ".ref")
    assert "font-family: inherit" in body
    assert "font-size: inherit" in body


def test_chart_fills_its_width_and_form_fields_align_to_the_top() -> None:
    css = (STATIC_DIR / "app.css").read_text()
    chart = _rule(css, ".chart")
    assert "width: 100%" in chart
    assert "max-height" not in chart
    assert "align-content: start" in _rule(css, ".field")


def _media_blocks(css: str, query: str) -> list[str]:
    """The bodies of every `@media <query> { ... }` block (brace-matched)."""
    bodies: list[str] = []
    start = 0
    while (at := css.find(f"@media {query} {{", start)) != -1:
        i = css.index("{", at) + 1
        depth = 1
        j = i
        while depth:
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            j += 1
        bodies.append(css[i : j - 1])
        start = j
    return bodies


def test_wide_screens_get_a_wide_page_and_a_card_grid() -> None:
    css = (STATIC_DIR / "app.css").read_text()
    wide = re.search(r"--wide:\s*([\d.]+)rem;", css)
    assert wide and 80 <= float(wide.group(1)) <= 84
    assert "max-width: var(--wide)" in _rule(css, ".page")
    desktop = "\n".join(_media_blocks(css, "(min-width: 64rem)"))
    assert "repeat(auto-fill, minmax(20rem, 1fr))" in _rule(desktop, "  .cards")
    assert "max-width: none" in _rule(desktop, "  .stream")
    # Forms and lists stay readable on the wide page.
    sources = re.search(r"max-width:\s*([\d.]+)rem", _rule(css, ".kind"))
    assert sources and float(sources.group(1)) <= 72
    settings = re.search(r"max-width:\s*([\d.]+)rem", _rule(css, ".settings"))
    assert settings and float(settings.group(1)) <= 72


# ---- Theme toggle (feedback-1) ------------------------------------------------------------------

DARK_MEDIA = "@media (prefers-color-scheme: dark)"


def test_stylesheet_has_auto_light_and_dark_theme_selectors() -> None:
    css = (STATIC_DIR / "app.css").read_text()
    auto = _media_blocks(css, "(prefers-color-scheme: dark)")
    assert any(':root:not([data-theme="light"])' in body for body in auto)
    forced_dark = _rule(css, ':root[data-theme="dark"]')
    forced_light = _rule(css, ':root[data-theme="light"]')
    assert "color-scheme: dark" in forced_dark
    assert "color-scheme: light" in forced_light
    # The forced-dark tokens are a copy of the device-dark ones: keep them identical.
    auto_tokens = re.findall(
        r"(--[\w-]+):\s*([^;]+);", _rule(auto[0], '  :root:not([data-theme="light"])')
    )
    dark_tokens = re.findall(r"(--[\w-]+):\s*([^;]+);", forced_dark)
    assert len(auto_tokens) >= 15
    assert auto_tokens == dark_tokens


def test_base_template_loads_theme_js_early_and_has_the_control() -> None:
    html = templates.get_template("base.html").render(
        csrf_token="tok", active="today", budget_reached=False
    )
    head = html[: html.index("</head>")]
    script = '<script src="/static/theme.js"></script>'
    assert script in head  # synchronous: no defer/async, so the theme is set before paint
    assert head.index(script) < head.index('<link rel="stylesheet"')
    assert '<div class="theme" role="group" aria-label="Theme">' in html
    for choice, label in (("auto", "Auto"), ("light", "Light"), ("dark", "Dark")):
        pressed = "true" if choice == "auto" else "false"
        assert (
            f'<button type="button" data-theme-choice="{choice}" aria-pressed="{pressed}">'
            f"{label}</button>" in html
        )


def test_theme_js_is_plain_and_guards_storage() -> None:
    js = (STATIC_DIR / "theme.js").read_text()
    for banned in (
        "eval",
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "Function(",
    ):
        assert banned not in js, banned
    assert "localStorage" in js
    unguarded = re.sub(r"try \{[^{}]*\} catch \(\w*\) \{[^{}]*\}", "", js)
    assert "localStorage" not in unguarded
    assert "DOMContentLoaded" in js
    assert "dataset.theme" in js
