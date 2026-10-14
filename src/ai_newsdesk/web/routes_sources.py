"""Sources: health overview, add, inline edit, mute and delete."""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ai_newsdesk.models import SourceConfig, SourceKind, SourceOverview, Trust
from ai_newsdesk.web.csrf import csrf_protect
from ai_newsdesk.web.deps import NO_STORE, is_htmx, kind_label, now_of, page, partial, store_of
from ai_newsdesk.web.forms import EDITABLE_KINDS, parse_source_form

router = APIRouter(include_in_schema=False, dependencies=[Depends(csrf_protect)])

KIND_ORDER = (
    SourceKind.FEED,
    SourceKind.X_PERSON,
    SourceKind.SEARCH,
    SourceKind.GITHUB,
    SourceKind.ARXIV,
    SourceKind.HN,
)
KIND_NOTES: dict[SourceKind, str] = {
    SourceKind.FEED: "Blogs and lab news, read directly from RSS or Atom.",
    SourceKind.X_PERSON: (
        "Posts found through research-engine search. Snippets only; X itself is never fetched."
    ),
    SourceKind.SEARCH: "Topic searches run through research-engine.",
    SourceKind.GITHUB: "New releases of these repositories.",
    SourceKind.ARXIV: "New papers, filtered hard before review.",
    SourceKind.HN: "Hacker News stories above the points threshold.",
}
_EMPTY_ADD = {"kind": "feed", "name": "", "trust": "trusted", "value": ""}


def _form_values(src: SourceConfig) -> dict[str, str]:
    spec = EDITABLE_KINDS.get(src.kind)
    return {
        "name": src.name,
        "trust": src.trust.value,
        "value": str(src.config.get(spec[0], "")) if spec else "",
    }


def _shared() -> dict[str, Any]:
    return {"editable_kinds": EDITABLE_KINDS, "trusts": list(Trust)}


async def _row(request: Request, source_id: int) -> SourceOverview:
    for row in await store_of(request).source_overview(now_of(request)):
        if row.source.id == source_id:
            return row
    raise HTTPException(status_code=404, detail="There is no such source.")


def _taken(kind: SourceKind, name: str) -> str:
    return f"A {kind.value} source called {name} already exists."


async def _name_taken(request: Request, kind: SourceKind, name: str, exclude: int | None) -> bool:
    sources = await store_of(request).list_sources(enabled_only=False)
    return any(s.kind == kind and s.name == name and s.id != exclude for s in sources)


async def _source(request: Request, source_id: int) -> SourceConfig:
    src = await store_of(request).get_source(source_id)
    if src is None:
        raise HTTPException(status_code=404, detail="There is no such source.")
    return src


async def _page(
    request: Request,
    *,
    add: dict[str, str] | None = None,
    add_errors: dict[str, str] | None = None,
    editing: int | None = None,
    edit_form: dict[str, str] | None = None,
    edit_errors: dict[str, str] | None = None,
    added: bool = False,
    status_code: int = 200,
) -> HTMLResponse:
    rows = await store_of(request).source_overview(now_of(request))
    groups = [
        (kind, kind_label(kind), KIND_NOTES[kind], [r for r in rows if r.source.kind == kind])
        for kind in KIND_ORDER
    ]
    return await page(
        request,
        "sources.html",
        {
            "active": "sources",
            "groups": [g for g in groups if g[3]],
            "enabled_count": sum(1 for r in rows if r.source.enabled),
            "muted_count": sum(1 for r in rows if not r.source.enabled),
            "add": add or _EMPTY_ADD,
            "add_errors": add_errors or {},
            "editing": editing,
            "edit_form": edit_form or {},
            "edit_errors": edit_errors or {},
            "added": added,
            **_shared(),
        },
        status_code=status_code,
    )


def _row_partial(request: Request, row: SourceOverview) -> HTMLResponse:
    return partial(request, "partials/source_row.html", {"row": row})


def _edit_partial(
    request: Request,
    row: SourceOverview,
    form: dict[str, str],
    errors: dict[str, str],
    status_code: int = 200,
) -> HTMLResponse:
    return partial(
        request,
        "partials/source_edit.html",
        {"row": row, "form": form, "errors": errors, **_shared()},
        status_code=status_code,
    )


@router.get("/sources", response_class=HTMLResponse)
async def sources_page(request: Request, added: int | None = None) -> HTMLResponse:
    return await _page(request, added=added is not None)


@router.post("/sources")
async def add_source(
    request: Request,
    kind: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    trust: Annotated[str, Form()] = "",
    value: Annotated[str, Form()] = "",
) -> Response:
    form = parse_source_form(kind=kind, name=name, trust=trust, value=value)
    if not form.ok or form.kind is None or form.trust is None:
        submitted = {"kind": kind, "name": name, "trust": trust, "value": value}
        return await _page(request, add=submitted, add_errors=form.errors, status_code=422)
    submitted = {"kind": kind, "name": name, "trust": trust, "value": value}
    if await _name_taken(request, form.kind, form.name, None):
        errors = {"name": _taken(form.kind, form.name)}
        return await _page(request, add=submitted, add_errors=errors, status_code=422)
    try:
        source_id = await store_of(request).upsert_source(
            form.kind, form.name, form.config, form.trust
        )
    except sqlite3.IntegrityError:
        errors = {"name": _taken(form.kind, form.name)}
        return await _page(request, add=submitted, add_errors=errors, status_code=422)
    return RedirectResponse(f"/sources?added={source_id}#source-{source_id}", status_code=303)


@router.get("/sources/{source_id}/edit", response_class=HTMLResponse)
async def edit_form(request: Request, source_id: int) -> HTMLResponse:
    src = await _source(request, source_id)
    if is_htmx(request):
        return _edit_partial(request, await _row(request, source_id), _form_values(src), {})
    return await _page(request, editing=source_id, edit_form=_form_values(src))


@router.get("/sources/{source_id}/row", response_class=HTMLResponse)
async def source_row(request: Request, source_id: int) -> HTMLResponse:
    return _row_partial(request, await _row(request, source_id))


@router.post("/sources/{source_id}")
async def edit_source(
    request: Request,
    source_id: int,
    name: Annotated[str, Form()] = "",
    trust: Annotated[str, Form()] = "",
    value: Annotated[str, Form()] = "",
) -> Response:
    existing = await _source(request, source_id)
    form = parse_source_form(
        kind=existing.kind.value, name=name, trust=trust, value=value, existing=existing
    )
    if not form.ok or form.trust is None:
        submitted = {"name": name, "trust": trust, "value": value}
        if is_htmx(request):
            return _edit_partial(
                request, await _row(request, source_id), submitted, form.errors, 422
            )
        return await _page(
            request,
            editing=source_id,
            edit_form=submitted,
            edit_errors=form.errors,
            status_code=422,
        )
    submitted = {"name": name, "trust": trust, "value": value}
    taken = await _name_taken(request, existing.kind, form.name, source_id)
    if not taken:
        try:
            # weight is left out (None), so step 1 keeps the weight learned from thumbs.
            await store_of(request).upsert_source(
                existing.kind,
                form.name,
                form.config,
                form.trust,
                enabled=existing.enabled,
                source_id=source_id,
            )
        except sqlite3.IntegrityError:
            taken = True
    if taken:
        errors = {"name": _taken(existing.kind, form.name)}
        if is_htmx(request):
            return _edit_partial(request, await _row(request, source_id), submitted, errors, 422)
        return await _page(
            request, editing=source_id, edit_form=submitted, edit_errors=errors, status_code=422
        )
    if is_htmx(request):
        return _row_partial(request, await _row(request, source_id))
    return RedirectResponse(f"/sources#source-{source_id}", status_code=303)


@router.post("/sources/{source_id}/mute")
async def toggle_mute(request: Request, source_id: int) -> Response:
    src = await _source(request, source_id)
    await store_of(request).upsert_source(
        src.kind, src.name, src.config, src.trust, enabled=not src.enabled, source_id=source_id
    )
    if is_htmx(request):
        return _row_partial(request, await _row(request, source_id))
    return RedirectResponse(f"/sources#source-{source_id}", status_code=303)


@router.post("/sources/{source_id}/delete")
async def delete_source(request: Request, source_id: int) -> Response:
    await _source(request, source_id)
    await store_of(request).delete_source(source_id)
    if is_htmx(request):
        return HTMLResponse("", headers=NO_STORE)
    return RedirectResponse("/sources", status_code=303)
