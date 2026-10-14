import pytest

from ai_newsdesk.normalise import (
    canonical_url,
    clean_text,
    excerpt_from_markdown,
    is_x_url,
    x_handle,
)

CANONICAL_CASES = [
    (
        "HTTPS://WWW.Example.COM/Path/?utm_source=x&utm_medium=y&id=5#frag",
        "https://example.com/Path?id=5",
    ),
    ("https://example.com/a/?fbclid=1&gclid=2&ref=hn", "https://example.com/a"),
    ("https://example.com/a?UTM_Campaign=z&page=2", "https://example.com/a?page=2"),
    ("https://example.com/a?ref_id=7", "https://example.com/a?ref_id=7"),
    ("https://twitter.com/Karpathy/status/123?s=20", "https://x.com/karpathy/status/123"),
    ("https://mobile.x.com/simonw/status/9/", "https://x.com/simonw/status/9"),
    ("https://www.x.com/simonw/status/9#m", "https://x.com/simonw/status/9"),
    ("http://example.com:80/x", "http://example.com/x"),
    ("https://example.com:8443/x", "https://example.com:8443/x"),
    ("https://example.com/", "https://example.com"),
    ("  https://example.com/a  ", "https://example.com/a"),
    ("not a url", "not a url"),
    ("mailto:someone@example.com", "mailto:someone@example.com"),
]


@pytest.mark.parametrize(("url", "expected"), CANONICAL_CASES)
def test_canonical_url(url: str, expected: str) -> None:
    assert canonical_url(url) == expected


@pytest.mark.parametrize(("url", "expected"), CANONICAL_CASES)
def test_canonical_url_is_idempotent(url: str, expected: str) -> None:
    assert canonical_url(canonical_url(url)) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://x.com/karpathy/status/123", "karpathy"),
        ("https://twitter.com/SimonW/status/9?s=20", "SimonW"),
        ("https://mobile.x.com/emollick/status/5", "emollick"),
        ("https://x.com/karpathy", None),
        ("https://example.com/karpathy/status/1", None),
    ],
)
def test_x_handle(url: str, expected: str | None) -> None:
    assert x_handle(url) == expected


def test_is_x_url() -> None:
    assert is_x_url("https://x.com/a/status/1")
    assert is_x_url("https://www.twitter.com/a")
    assert not is_x_url("https://notx.com/a")
    assert not is_x_url("https://example.com/x.com")


def test_clean_text_strips_tags_entities_and_whitespace() -> None:
    raw = "<p>Hello&nbsp;<b>world</b></p>\n\n  &amp; more"
    assert clean_text(raw) == "Hello world & more"


def test_clean_text_limit_and_empty() -> None:
    assert clean_text("abc def ghi", 7) == "abc def"
    assert clean_text("abc def ghi", 8) == "abc def"
    assert clean_text(None) == ""
    assert clean_text("") == ""


ANNOUNCEMENT = (
    "Anthropic today released a new model that is faster and cheaper than before, says the"
)
PRICING = (
    "The model is available in the API from today, with pricing at one dollar per million"
    " input tokens."
)
ARTICLE_MARKDOWN = f"""[Home](/) | [Blog](/blog)
![hero](https://img.example/x.png)
# Short title
{ANNOUNCEMENT} [announcement](https://a.example/c).
Subscribe
{PRICING}
"""


def test_excerpt_from_markdown_keeps_prose_only() -> None:
    assert excerpt_from_markdown(ARTICLE_MARKDOWN) == f"{ANNOUNCEMENT} announcement. {PRICING}"


def test_excerpt_from_markdown_is_capped() -> None:
    excerpt = excerpt_from_markdown(("x" * 70 + "\n") * 50)
    assert 1400 < len(excerpt) <= 1500
    assert excerpt_from_markdown(None) == ""
