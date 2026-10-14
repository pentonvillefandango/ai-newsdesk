from __future__ import annotations

from ai_newsdesk.models import SourceConfig, SourceKind, Trust
from ai_newsdesk.web.forms import parse_source_form


def test_valid_feed() -> None:
    form = parse_source_form(
        kind="feed",
        name="  Example   Blog ",
        trust="trusted",
        value=" https://blog.example/feed.xml ",
    )
    assert form.ok
    assert form.kind == SourceKind.FEED
    assert form.trust == Trust.TRUSTED
    assert form.name == "Example Blog"
    assert form.config == {"url": "https://blog.example/feed.xml"}


def test_feed_must_be_http_or_https() -> None:
    for bad in (
        "ftp://blog.example/feed",
        "blog.example/feed",
        "javascript:alert(1)",
        "https://",
        "",
    ):
        form = parse_source_form(kind="feed", name="Blog", trust="trusted", value=bad)
        assert form.errors == {
            "value": "Use a feed address that starts with http:// or https://."
        }, bad


def test_x_handle_rules() -> None:
    assert parse_source_form(
        kind="x_person", name="A", trust="trusted", value="@karpathy"
    ).config == {"handle": "karpathy"}
    assert parse_source_form(
        kind="x_person", name="A", trust="trusted", value="a_b_123456789012"
    ).errors == {"value": "An X handle is 1 to 15 letters, digits or underscores."}
    for bad in ("", "has space", "dash-name", "toolonghandle_123"):
        assert (
            "value"
            in parse_source_form(kind="x_person", name="A", trust="trusted", value=bad).errors
        ), bad


def test_search_and_github() -> None:
    assert parse_source_form(
        kind="search", name="S", trust="discovery", value=" new  AI model "
    ).config == {"query": "new AI model"}
    assert (
        "value" in parse_source_form(kind="search", name="S", trust="discovery", value=" ").errors
    )
    assert parse_source_form(
        kind="github", name="G", trust="trusted", value="https://github.com/org/repo/"
    ).config == {"repo": "org/repo"}
    assert (
        "value"
        in parse_source_form(kind="github", name="G", trust="trusted", value="not a repo").errors
    )


def test_kind_trust_and_name_are_checked() -> None:
    form = parse_source_form(kind="hn", name="", trust="best", value="x")
    assert set(form.errors) == {"kind", "trust", "name"}
    assert "value" not in form.errors
    assert parse_source_form(
        kind="feed", name="n" * 81, trust="primary", value="https://a.example"
    ).errors == {"name": "Give the source a name of 1 to 80 characters."}


def test_editing_keeps_kind_and_unedited_config() -> None:
    hn = SourceConfig(
        id=4,
        kind=SourceKind.HN,
        name="Hacker News",
        config={"min_points": 100},
        trust=Trust.DISCOVERY,
    )
    form = parse_source_form(
        kind="feed", name="HN front page", trust="discovery", value="", existing=hn
    )
    assert form.ok
    assert form.kind == SourceKind.HN
    assert form.config == {"min_points": 100}
