"""All page routers in one place (login and logout are installed by `web.security`)."""

from __future__ import annotations

from fastapi import APIRouter

from ai_newsdesk.web import routes_health


def build_router() -> APIRouter:
    router = APIRouter()
    for sub in (routes_health.router,):
        router.include_router(sub)
    return router
