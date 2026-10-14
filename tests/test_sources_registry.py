import re
from pathlib import Path
from typing import Any

import httpx
import yaml

from ai_newsdesk.models import SourceConfig, SourceKind, Trust
from ai_newsdesk.normalise import is_x_url
from ai_newsdesk.sources.registry import build_sources
from tests.fakes import FakeResearchEngine

SOURCES_YAML = Path(__file__).resolve().parents[1] / "config" / "sources.yaml"


def seed_entries() -> list[dict[str, Any]]:
    loaded: dict[str, list[dict[str, Any]]] = yaml.safe_load(SOURCES_YAML.read_text())
    entries = loaded["sources"]
    assert isinstance(entries, list)
    return entries


def seed_configs() -> list[SourceConfig]:
    return [SourceConfig(id=i, **entry) for i, entry in enumerate(seed_entries(), start=1)]


def test_every_kind_has_a_matching_adapter() -> None:
    sources = build_sources(httpx.AsyncClient(), FakeResearchEngine())
    assert set(sources) == set(SourceKind)
    for kind, source in sources.items():
        assert source.kind == kind


def test_seed_file_loads_into_source_configs() -> None:
    configs = seed_configs()
    assert len(configs) >= 60
    assert len({c.name for c in configs}) == len(configs), "names must be unique"
    assert {c.kind for c in configs} == set(SourceKind)
    assert all(c.enabled for c in configs)


def test_seed_configs_are_well_formed() -> None:
    for c in seed_configs():
        if c.kind in (SourceKind.FEED,):
            url = c.config["url"]
            assert isinstance(url, str) and url.startswith("https://") and not is_x_url(url), c.name
        if c.kind == SourceKind.X_PERSON:
            handle = c.config["handle"]
            assert isinstance(handle, str) and re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle), c.name
        if c.kind == SourceKind.SEARCH:
            assert c.config.get("query") or c.config.get("site"), c.name
        if c.kind == SourceKind.GITHUB:
            repo = c.config["repo"]
            assert isinstance(repo, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", repo), c.name


def test_seed_arxiv_sources_carry_keywords() -> None:
    arxiv = [c for c in seed_configs() if c.kind == SourceKind.ARXIV]
    assert arxiv
    for c in arxiv:
        keywords = c.config.get("keywords")
        assert isinstance(keywords, list) and keywords, c.name
        assert all(isinstance(k, str) and k.strip() for k in keywords), c.name
        assert len({k.lower() for k in keywords}) == len(keywords), c.name


def test_seed_trust_levels() -> None:
    by_name = {c.name: c for c in seed_configs()}
    assert by_name["OpenAI"].trust == Trust.PRIMARY
    assert by_name["Simon Willison"].trust == Trust.TRUSTED
    assert by_name["X: @karpathy"].trust == Trust.TRUSTED
    for c in by_name.values():
        if c.kind in (SourceKind.ARXIV, SourceKind.HN) or c.name.startswith("Search: "):
            assert c.trust == Trust.DISCOVERY, c.name


def test_seed_x_handles_are_unique() -> None:
    handles = [
        str(c.config["handle"]).lower() for c in seed_configs() if c.kind == SourceKind.X_PERSON
    ]
    assert len(handles) == len(set(handles))


def test_seed_has_no_seo_bait_searches() -> None:
    queries = [
        str(c.config.get("query", "")).lower()
        for c in seed_configs()
        if c.kind == SourceKind.SEARCH
    ]
    assert "best ai tools 2026" not in queries
    assert "ai news today" not in queries
