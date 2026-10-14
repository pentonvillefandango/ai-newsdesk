"""All page routers in one place (login and logout are installed by `web.security`)."""

from __future__ import annotations

from fastapi import APIRouter

from ai_newsdesk.web import (
    routes_briefing,
    routes_compare,
    routes_health,
    routes_settings,
    routes_sources,
    routes_today,
    routes_x,
)


def build_router() -> APIRouter:
    router = APIRouter()
    for sub in (
        routes_health.router,
        routes_today.router,
        routes_briefing.router,
        routes_x.router,
        routes_sources.router,
        routes_settings.router,
        routes_compare.router,
    ):
        router.include_router(sub)
    return router
