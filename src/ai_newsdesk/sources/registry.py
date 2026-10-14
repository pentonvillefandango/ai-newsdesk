"""One adapter per SourceKind. Only app.py calls this; everything else receives the dict."""

from __future__ import annotations

import httpx

from ai_newsdesk.models import SourceKind
from ai_newsdesk.protocols import Clock, ResearchEngine, Source
from ai_newsdesk.sources.arxiv import ArxivSource
from ai_newsdesk.sources.feed import FeedSource
from ai_newsdesk.sources.github import GitHubReleasesSource
from ai_newsdesk.sources.hn import HNSource
from ai_newsdesk.sources.search import SearchSource
from ai_newsdesk.sources.x_person import XPersonSource


def build_sources(
    http: httpx.AsyncClient, research_engine: ResearchEngine, clock: Clock | None = None
) -> dict[SourceKind, Source]:
    return {
        SourceKind.FEED: FeedSource(http),
        SourceKind.X_PERSON: XPersonSource(research_engine, clock),
        SourceKind.SEARCH: SearchSource(research_engine, clock),
        SourceKind.ARXIV: ArxivSource(http),
        SourceKind.GITHUB: GitHubReleasesSource(http),
        SourceKind.HN: HNSource(http),
    }
