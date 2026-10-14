"""GitHub releases of one repository via its Atom feed (kind `github`)."""

from __future__ import annotations

import re
from typing import Any

from ai_newsdesk.models import SourceConfig, SourceKind
from ai_newsdesk.normalise import TITLE_MAX, clean_text
from ai_newsdesk.sources.base import SourceConfigError, cfg_str
from ai_newsdesk.sources.feed import FeedSource

_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def repo_of(src: SourceConfig) -> str:
    repo = cfg_str(src, "repo").strip().strip("/")
    if not _REPO.match(repo):
        raise SourceConfigError(
            f"source {src.id} ({src.name}): config 'repo' must look like owner/name"
        )
    return repo


class GitHubReleasesSource(FeedSource):
    """Config: `repo` ("owner/name", required), `max` (default 5), `lookback_hours` (default 24).
    Same ETag/Last-Modified handling as FeedSource. Titles are prefixed with the repo name."""

    kind = SourceKind.GITHUB
    default_max = 5

    def feed_url(self, src: SourceConfig) -> str:
        return f"https://github.com/{repo_of(src)}/releases.atom"

    def entry_title(self, src: SourceConfig, entry: Any) -> str:
        release = clean_text(entry.get("title") or "", TITLE_MAX)
        return clean_text(f"{repo_of(src)} {release}", TITLE_MAX) if release else ""
