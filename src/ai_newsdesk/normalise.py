"""URL canonicalisation and text cleaning shared by sources, dedupe and enrichment."""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TITLE_MAX = 300
SNIPPET_MAX = 1500
EXCERPT_MAX = 1500

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "ref"})
_X_HOSTS = frozenset({"x.com", "twitter.com", "mobile.x.com", "mobile.twitter.com"})
_X_STATUS = re.compile(
    r"^https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/([^/?#]+)/status/", re.IGNORECASE
)
_DEFAULT_PORTS = {("http", 80), ("https", 443)}


def _is_tracking(key: str) -> bool:
    lowered = key.lower()
    return lowered.startswith("utm_") or lowered in _TRACKING_PARAMS


def canonical_url(url: str) -> str:
    """One spelling per page, used for dedupe. Non-http(s) or unparseable input comes back
    stripped."""
    raw = url.strip()
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return raw
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return raw
    host = host.removeprefix("www.")
    path = parts.path.rstrip("/")
    if host in _X_HOSTS:
        # X: one host, handles are case-insensitive, and the query (?s=20, ?t=...) is share
        # tracking.
        return urlunsplit(("https", "x.com", path.lower(), "", ""))
    netloc = host if port is None or (scheme, port) in _DEFAULT_PORTS else f"{host}:{port}"
    kept = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(k)
    ]
    return urlunsplit((scheme, netloc, path, urlencode(kept), ""))


def x_handle(url: str) -> str | None:
    """The author handle of an X/Twitter status URL, as written in the URL; None for anything
    else."""
    match = _X_STATUS.match(url.strip())
    return match.group(1) if match else None


def is_x_url(url: str) -> bool:
    try:
        host = (urlsplit(url.strip()).hostname or "").lower()
    except ValueError:
        return False
    return host.removeprefix("www.") in _X_HOSTS


def clean_text(text: str | None, limit: int | None = None) -> str:
    """Strip HTML tags, unescape entities, collapse whitespace, then cap at `limit` characters."""
    if not text:
        return ""
    out = _TAG.sub(" ", text)
    out = html.unescape(out)
    out = _WS.sub(" ", out).strip()
    if limit is not None and len(out) > limit:
        out = out[:limit].rstrip()
    return out


def excerpt_from_markdown(markdown: str | None, limit: int = EXCERPT_MAX) -> str:
    """Article prose from research-engine Markdown: no images, links as text, short (menu) lines
    dropped."""
    text = _MD_IMAGE.sub(" ", markdown or "")
    text = _MD_LINK.sub(r"\1", text)
    lines = [line.strip() for line in text.splitlines() if len(line.strip()) > 60]
    return clean_text(" ".join(lines), limit)
