"""Template environment, view helpers and page rendering shared by every route module."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader

from ai_newsdesk.models import Category, SourceConfig, SourceKind, StorySource, Trust
from ai_newsdesk.runtime_settings import ApiKeys, load_runtime_settings
from ai_newsdesk.store import Store

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"
NO_STORE = {"Cache-Control": "no-store"}

CATEGORY_LABELS: dict[Category, str] = {
    Category.MODEL_RELEASE: "Model releases",
    Category.RESEARCH: "Research",
    Category.PRODUCT: "Products",
    Category.POLICY: "Policy",
    Category.INDUSTRY: "Industry",
    Category.OPINION: "Opinion",
    Category.OTHER: "Other",
}
KIND_LABELS: dict[SourceKind, str] = {
    SourceKind.FEED: "Feeds",
    SourceKind.X_PERSON: "People on X",
    SourceKind.SEARCH: "Searches",
    SourceKind.GITHUB: "GitHub releases",
    SourceKind.ARXIV: "arXiv",
    SourceKind.HN: "Hacker News",
}
TRUST_LABELS: dict[Trust, str] = {
    Trust.PRIMARY: "Primary",
    Trust.TRUSTED: "Trusted",
    Trust.DISCOVERY: "Discovery",
}
KIND_ICONS: dict[SourceKind, str] = {
    SourceKind.FEED: "i-rss",
    SourceKind.X_PERSON: "i-x-logo",
    SourceKind.HN: "i-hn",
}


def ago(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return "never"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    seconds = (now - dt).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    days = int(seconds // 86400)
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    return f"{dt.day} {dt:%b %Y}"


def day_label(d: date) -> str:
    return f"{d:%A} {d.day} {d:%B}"


def short_date(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def usd(value: float) -> str:
    if value <= 0:
        return "$0.00"
    if value < 0.01:
        return "under $0.01"
    return f"${value:,.2f}"


def safe_url(url: str) -> str:
    """Outbound links come from untrusted feeds: only absolute http(s) URLs survive."""
    candidate = url.strip()
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return "#"
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return "#"
    return candidate


def signal_of(score: float) -> int:
    return max(0, min(10, round(score)))


def unique_sources(sources: Iterable[StorySource]) -> list[StorySource]:
    seen: set[str] = set()
    out: list[StorySource] = []
    for src in sources:
        if src.source_name not in seen:
            seen.add(src.source_name)
            out.append(src)
    return out


def category_label(category: Category | str) -> str:
    try:
        return CATEGORY_LABELS[Category(category)]
    except ValueError:
        return str(category)


def kind_label(kind: SourceKind | str) -> str:
    return KIND_LABELS.get(SourceKind(kind), str(kind))


def trust_label(trust: Trust | str) -> str:
    return TRUST_LABELS.get(Trust(trust), str(trust))


def icon_for(kind: SourceKind | str) -> str | None:
    return KIND_ICONS.get(SourceKind(kind))


def source_detail(src: SourceConfig) -> str:
    cfg = src.config
    match src.kind:
        case SourceKind.FEED:
            return str(cfg.get("url", ""))
        case SourceKind.X_PERSON:
            return f"@{cfg.get('handle', '')}"
        case SourceKind.SEARCH:
            return f"Search: {cfg.get('query', '')}"
        case SourceKind.GITHUB:
            return f"github.com/{cfg.get('repo', '')}"
        case SourceKind.ARXIV:
            cats = cfg.get("categories", [])
            return ", ".join(cats) if isinstance(cats, list) else str(cats)
        case SourceKind.HN:
            return "Front-page stories above the points threshold"


templates = Jinja2Templates(
    env=Environment(
        loader=FileSystemLoader(WEB_DIR / "templates"),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
)
templates.env.globals.update(
    ago=ago,
    day_label=day_label,
    short_date=short_date,
    usd=usd,
    signal_of=signal_of,
    unique_sources=unique_sources,
    category_label=category_label,
    kind_label=kind_label,
    trust_label=trust_label,
    icon_for=icon_for,
    source_detail=source_detail,
)
templates.env.filters["safe_url"] = safe_url
templates.env.filters["thousands"] = lambda n: f"{n:,}"  # 12345 -> "12,345"


def store_of(request: Request) -> Store:
    return request.app.state.store


def api_keys_of(request: Request) -> ApiKeys:
    return request.app.state.api_keys


def now_of(request: Request) -> datetime:
    return request.app.state.ctx.clock.now()


def zone_of(request: Request) -> ZoneInfo:
    return ZoneInfo(request.app.state.settings.tz)


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


async def page(
    request: Request, name: str, context: Mapping[str, Any], *, status_code: int = 200
) -> HTMLResponse:
    """A full page: adds the CSRF token, clock, zone, runtime settings and the budget flag."""
    store = store_of(request)
    now = now_of(request)
    runtime = await load_runtime_settings(store)
    spent = await store.month_spend(now, request.app.state.settings.tz)
    full: dict[str, Any] = {
        "csrf_token": request.state.csrf_token,
        "now": now,
        "zone": zone_of(request),
        "runtime": runtime,
        "budget_reached": spent >= runtime.monthly_budget_usd,
        "active": "",
    }
    full.update(context)
    return templates.TemplateResponse(
        request, name, full, status_code=status_code, headers=NO_STORE
    )


def partial(
    request: Request, name: str, context: Mapping[str, Any], *, status_code: int = 200
) -> HTMLResponse:
    """An htmx fragment: no layout, same escaping rules."""
    full: dict[str, Any] = {
        "csrf_token": request.state.csrf_token,
        "now": now_of(request),
        "zone": zone_of(request),
    }
    full.update(context)
    return templates.TemplateResponse(
        request, name, full, status_code=status_code, headers=NO_STORE
    )
