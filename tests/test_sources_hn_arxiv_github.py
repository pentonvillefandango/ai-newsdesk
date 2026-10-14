import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import respx

from ai_newsdesk.models import SourceConfig, SourceKind, Trust
from ai_newsdesk.sources.arxiv import ARXIV_API, ArxivSource, format_authors
from ai_newsdesk.sources.github import GitHubReleasesSource
from ai_newsdesk.sources.hn import HN_SEARCH_URL, HNSource

FIXTURES = Path(__file__).parent / "fixtures"
HN_JSON = json.loads((FIXTURES / "hn" / "search_by_date.json").read_text())
ARXIV_XML = (FIXTURES / "feeds" / "arxiv_query.xml").read_bytes()
GITHUB_ATOM = (FIXTURES / "feeds" / "github_releases.atom").read_bytes()
SINCE = datetime(2026, 10, 14, 6, 0, tzinfo=UTC)


def make_src(kind: SourceKind, **config: Any) -> SourceConfig:
    return SourceConfig(id=31, kind=kind, name=str(kind), config=config, trust=Trust.DISCOVERY)


async def test_hn_stories(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(HN_SEARCH_URL).mock(return_value=httpx.Response(200, json=HN_JSON))
    result = await HNSource(httpx.AsyncClient()).collect(make_src(SourceKind.HN), SINCE)
    assert result.error is None
    assert [(c.url, c.hn_points) for c in result.candidates] == [
        ("https://www.anthropic.com/news/claude-haiku-5-5", 512),
        ("https://news.ycombinator.com/item?id=41000002", 150),
    ]
    ask = result.candidates[1]
    assert ask.author == "builder"
    assert ask.snippet == "We are building & testing agents."
    assert ask.published_at == datetime(2026, 10, 13, 22, 30, tzinfo=UTC)
    params = route.calls.last.request.url.params
    created_after = int((SINCE - timedelta(hours=24)).timestamp())
    assert params["query"] == "AI"
    assert params["tags"] == "story"
    assert params["numericFilters"] == f"points>=100,created_at_i>{created_after}"
    assert params["hitsPerPage"] == "30"


async def test_hn_min_points_and_queries_from_config(respx_mock: respx.MockRouter) -> None:
    sent: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=HN_JSON)

    respx_mock.get(HN_SEARCH_URL).mock(side_effect=reply)
    source = HNSource(httpx.AsyncClient())
    result = await source.collect(
        make_src(SourceKind.HN, queries=["AI", "LLM"], min_points=250), SINCE
    )
    assert [request.url.params["query"] for request in sent] == ["AI", "LLM"]
    assert sent[0].url.params["numericFilters"].startswith("points>=250,")
    assert len(result.candidates) == 2  # the same stories from both queries are kept once


async def test_hn_http_error_is_recorded(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(HN_SEARCH_URL).mock(return_value=httpx.Response(503))
    result = await HNSource(httpx.AsyncClient()).collect(make_src(SourceKind.HN), SINCE)
    assert result.candidates == []
    assert result.error is not None and result.error.startswith("HTTPStatusError")


async def test_arxiv_papers(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(ARXIV_API).mock(return_value=httpx.Response(200, content=ARXIV_XML))
    result = await ArxivSource(httpx.AsyncClient()).collect(
        make_src(SourceKind.ARXIV, max=25), SINCE
    )
    assert result.error is None
    [paper] = result.candidates
    assert paper.url == "http://arxiv.org/abs/2610.01234v1"
    assert paper.title == "Sparse Mixture of Experts at Scale"
    assert paper.author == "Ada Lovelace, Alan Turing, Grace Hopper et al."
    assert (
        paper.snippet
        == "We study sparse mixture-of-experts language models at scale and report new results."
    )
    assert paper.published_at == datetime(2026, 10, 13, 17, 59, 59, tzinfo=UTC)
    assert str(route.calls.last.request.url) == (
        "https://export.arxiv.org/api/query?search_query=cat:cs.CL+OR+cat:cs.AI+OR+cat:cs.LG"
        "&sortBy=submittedDate&sortOrder=descending&max_results=25"
    )


async def test_arxiv_categories_from_config(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(ARXIV_API).mock(return_value=httpx.Response(200, content=ARXIV_XML))
    await ArxivSource(httpx.AsyncClient()).collect(
        make_src(SourceKind.ARXIV, categories=["cs.CV"]), SINCE
    )
    assert "search_query=cat:cs.CV&" in str(route.calls.last.request.url)


def test_format_authors() -> None:
    assert format_authors([]) is None
    assert format_authors(["A"]) == "A"
    assert format_authors(["A", "B", "C"]) == "A, B, C"
    assert format_authors(["A", "B", "C", "D"]) == "A, B, C et al."


async def test_github_releases(respx_mock: respx.MockRouter) -> None:
    url = "https://github.com/vllm-project/vllm/releases.atom"
    respx_mock.get(url).mock(return_value=httpx.Response(200, content=GITHUB_ATOM))
    result = await GitHubReleasesSource(httpx.AsyncClient()).collect(
        make_src(SourceKind.GITHUB, repo="vllm-project/vllm"), SINCE
    )
    assert result.error is None
    [release] = result.candidates
    assert release.title == "vllm-project/vllm v0.12.0"
    assert release.url == "https://github.com/vllm-project/vllm/releases/tag/v0.12.0"
    assert release.author == "release-bot"
    assert release.snippet == "Highlights Faster MoE kernels."


async def test_github_bad_repo_is_a_config_error() -> None:
    result = await GitHubReleasesSource(httpx.AsyncClient()).collect(
        make_src(SourceKind.GITHUB, repo="not a repo"), SINCE
    )
    assert result.error is not None and result.error.startswith("SourceConfigError")
