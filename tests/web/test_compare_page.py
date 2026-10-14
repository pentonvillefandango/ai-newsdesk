"""The Compare page (multi-provider spec 4): setup, the maximum-cost check, background runs
under the pipeline lock, results with labels and preferences, "Use this model" and history."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_newsdesk import compare
from ai_newsdesk.app import create_app
from ai_newsdesk.clock import FixedClock
from ai_newsdesk.models import Category, ItemStatus, StoryText, Verdict
from ai_newsdesk.runtime_settings import (
    API_KEY_NAMES,
    RuntimeSettings,
    load_runtime_settings,
    update_runtime_settings,
)
from ai_newsdesk.store import Store
from ai_newsdesk.store.records import CompareModel, CompareResult
from ai_newsdesk.web import routes_compare
from ai_newsdesk.web.model_choices import JUDGE_WARNING
from tests.fake_openai import completion
from tests.web import seed
from tests.web.fakes import fake_ctx_factory
from tests.web.support import (
    BASE_URL,
    CONFIG_DIR,
    HX,
    NOW,
    csrf_token,
    fixture_catalogue,
    login,
    make_settings,
    run,
)

OPENROUTER_KEY = "sk-or-" + "r" * 40
OPENAI_KEY = "sk-oa-" + "o" * 40
GEMINI = CompareModel(provider="openrouter", model="google/gemini-3.8-flash")
NANO = CompareModel(provider="openai", model="gpt-5.4-nano")
HAIKU = CompareModel(provider="anthropic", model="claude-haiku-5-5")
TWO_MODELS = {
    "m1_provider": "openrouter",
    "m1_model": "google/gemini-3.8-flash",
    "m2_provider": "openai",
    "m2_model": "gpt-5.4-nano",
}


class Provider:
    """A chat-completions endpoint that answers every judge or writer batch it is sent."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.summary = "A plain summary."
        self.hang = False  # accept the request and never answer

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.hang:
            await asyncio.sleep(3600)
        user = json.loads(request.content)["messages"][-1]["content"]
        items = [int(i) for i in re.findall(r'<item id="(\d+)">', user)]
        stories = [int(i) for i in re.findall(r'<story id="(\d+)">', user)]
        if items:
            answer: dict[str, Any] = {
                "verdicts": [
                    {
                        "item_id": i,
                        "signal": 7,
                        "junk": False,
                        "category": "research",
                        "same_story_id": None,
                    }
                    for i in items
                ]
            }
        else:
            answer = {
                "stories": [
                    {"story_id": i, "summary": self.summary, "why_it_matters": None}
                    for i in stories
                ]
            }
        return httpx.Response(
            200, json=completion(json.dumps(answer), prompt_tokens=900, cost=0.001)
        )


async def _drain(app: FastAPI) -> None:
    while app.state.background:
        await asyncio.gather(*list(app.state.background))


@pytest.fixture
def provider() -> Provider:
    return Provider()


@pytest.fixture
def capp(tmp_path: Path, provider: Provider) -> FastAPI:
    async def ok(key: str, runtime: RuntimeSettings) -> str | None:
        return None

    return create_app(
        make_settings(tmp_path),
        ctx_factory=fake_ctx_factory,
        key_testers={name: ok for name in API_KEY_NAMES},
        config_dir=CONFIG_DIR,
        clock=FixedClock(NOW),
        catalogue_fetch=fixture_catalogue,
        model_http_client=httpx.AsyncClient(transport=httpx.MockTransport(provider.handle)),
    )


@pytest.fixture
def cclient(capp: FastAPI) -> Iterator[TestClient]:
    with TestClient(capp, base_url=BASE_URL) as c:
        login(c)
        run(c, _drain, capp)  # the start-up model-list refresh (fixture)
        yield c


def _post(client: TestClient, path: str, data: dict[str, str], **kw: Any) -> Any:
    return client.post(
        path, data={**data, "csrf_token": csrf_token(client)}, follow_redirects=False, **kw
    )


def _save_keys(client: TestClient) -> None:
    for name, key in (("openrouter", OPENROUTER_KEY), ("openai", OPENAI_KEY)):
        assert _post(client, f"/settings/keys/{name}", {"key": key}).status_code == 303


def _store(app: FastAPI) -> Store:
    return app.state.store


async def _items(store: Store, n: int, *, title: str = "Lab item number") -> list[int]:
    src = await seed.add_source(store, name="Lab blog")
    ids = []
    for i in range(n):
        ids.append(
            await seed.add_item(
                store,
                source_id=src,
                url=f"https://lab.example/{i}",
                title=f"{title} {i}",
                at=NOW - timedelta(minutes=5 * (i + 1)),
                status=ItemStatus.QUEUED,
            )
        )
    return ids


async def _stories(store: Store, n: int) -> list[int]:
    src = await seed.add_source(store, name="Story blog")
    return [
        await seed.add_story(
            store,
            at=NOW - timedelta(hours=1 + i),
            headline=f"Story headline {i}",
            sources=[(src, f"https://story.example/{i}", f"Story headline {i}")],
            signal=9 - i,
            summary=f"Production summary {i}.",
        )
        for i in range(n)
    ]


async def _snapshot(store: Store) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    items = [tuple(r) for r in await store._all("SELECT * FROM items ORDER BY id")]
    stories = [tuple(r) for r in await store._all("SELECT * FROM stories ORDER BY id")]
    return items, stories


async def _finished_run(
    store: Store,
    job: str,
    models: list[CompareModel],
    ids: list[int],
    outputs: dict[str, list[dict[str, Any] | None]],
    *,
    status: str = "done",
) -> int:
    run_id = await store.create_compare_run(job, models, ids, 0.25, NOW)  # type: ignore[arg-type]
    rows = [
        CompareResult(
            model_key=key,
            subject_id=sid,
            output=out,
            ok=out is not None,
            latency_ms=800,
            cost_usd=0.001,
        )
        for key, outs in outputs.items()
        for sid, out in zip(ids, outs, strict=True)
    ]
    await store.add_compare_results(run_id, rows)
    await store.finish_compare_run(run_id, status, NOW, 0.004, None)  # type: ignore[arg-type]
    return run_id


def _verdict(item_id: int, signal: int, junk: bool = False) -> dict[str, Any]:
    return Verdict(
        item_id=item_id, signal=signal, junk=junk, category=Category.RESEARCH
    ).model_dump(mode="json")


# ---- the page and its setup form ----------------------------------------------------------


def test_compare_is_in_the_nav_after_settings(cclient: TestClient) -> None:
    page = cclient.get("/compare").text
    nav = re.search(r'<nav class="nav".*?</nav>', page, re.S)
    assert nav is not None
    links = re.findall(r'href="(/[a-z]*)"', nav.group(0))
    assert links[-2:] == ["/settings", "/compare"]
    assert '<a href="/compare" aria-current="page">Compare</a>' in page


def test_the_setup_form_reuses_the_settings_model_controls(cclient: TestClient) -> None:
    page = cclient.get("/compare").text
    for key in ("m1", "m2", "m3", "m4"):
        assert f'data-provider-for="{key}"' in page
        assert f'name="{key}_model_other"' in page
    assert '<option value="">Not used</option>' in page
    assert "google/gemini-3.8-flash ($0.75 in, $3.75 out per million tokens)" in page
    assert '<datalist id="models-openrouter">' in page
    assert 'name="job"' in page and 'value="judge"' in page and 'value="writer"' in page
    assert 'name="cap_usd"' in page and 'value="0.25"' in page
    assert 'name="sample_size"' in page
    assert "/static/settings.js" in page
    # Run waits for a cost check
    assert re.search(r"<button[^>]*formaction=\"/compare/run\"[^>]*disabled", page)


def test_check_cost_shows_the_maximum_and_enables_run(
    capp: FastAPI, cclient: TestClient, provider: Provider
) -> None:
    _save_keys(cclient)
    run(cclient, _items, _store(capp), 12)
    r = _post(cclient, "/compare/check", {"job": "judge", **TWO_MODELS, "cap_usd": "0.25"})
    assert r.status_code == 200, r.text
    assert "Maximum cost" in r.text
    assert "12 items" in r.text
    assert re.search(r"<button[^>]*formaction=\"/compare/run\"(?![^>]*disabled)[^>]*>", r.text)
    assert provider.requests == []  # checking never calls a model


def test_check_cost_matches_the_engine(capp: FastAPI, cclient: TestClient) -> None:
    _save_keys(cclient)
    run(cclient, _items, _store(capp), 3)

    async def expected() -> float:
        sample = await compare.judge_sample(_store(capp), NOW, 30)
        async with capp.state.compare_clients() as client_for:
            clients = [client_for(m.provider, m.model) for m in (GEMINI, NANO)]
            return compare.max_cost_usd("judge", clients, sample)

    want = run(cclient, expected)
    r = _post(cclient, "/compare/check", {"job": "judge", **TWO_MODELS})
    assert r.status_code == 200
    assert f"${want:.4f}" in r.text


def test_check_cost_over_the_cap_keeps_run_disabled(capp: FastAPI, cclient: TestClient) -> None:
    _save_keys(cclient)
    run(cclient, _items, _store(capp), 30)
    r = _post(cclient, "/compare/check", {"job": "judge", **TWO_MODELS, "cap_usd": "0.01"})
    assert "over the cap" in r.text
    assert re.search(r"<button[^>]*formaction=\"/compare/run\"[^>]*disabled", r.text)


@pytest.mark.parametrize(
    ("form", "field", "message"),
    [
        ({"job": "judge", "m1_provider": "openrouter", "m1_model": "google/gemini-3.8-flash"},
         "models", "Choose at least two models."),
        ({"job": "judge", **TWO_MODELS, "m3_provider": "openrouter",
          "m3_model": "google/gemini-3.8-flash"}, "m3_model", "already in this comparison"),
        ({"job": "judge", **TWO_MODELS, "m2_model": "", "m2_model_other": "nope-1"},
         "m2_model", "OpenAI"),
        ({"job": "briefing", **TWO_MODELS}, "job", "Choose Judge or Summary writer."),
        ({"job": "judge", **TWO_MODELS, "cap_usd": "9"}, "cap_usd", "$0.01 to $5.00"),
        ({"job": "judge", **TWO_MODELS, "cap_usd": "0.001"}, "cap_usd", "$0.01 to $5.00"),
        ({"job": "judge", **TWO_MODELS, "sample_size": "101"}, "sample_size", "1 to 100"),
        ({"job": "writer", **TWO_MODELS, "sample_size": "16"}, "sample_size", "1 to 15"),
    ],
)  # fmt: skip
def test_the_setup_is_validated(
    cclient: TestClient, provider: Provider, form: dict[str, str], field: str, message: str
) -> None:
    _save_keys(cclient)
    r = _post(cclient, "/compare/check", form)
    assert r.status_code == 422
    assert f'id="{field}-error"' in r.text
    assert message in r.text.replace("&#39;", "'")
    assert provider.requests == []


def test_a_missing_key_is_named(cclient: TestClient) -> None:
    r = _post(cclient, "/compare/check", {"job": "judge", **TWO_MODELS})
    assert r.status_code == 422
    assert "Add your OpenRouter API key first." in r.text
    assert "Add your OpenAI API key first." in r.text


def test_the_cap_cannot_pass_the_money_left_this_month(capp: FastAPI, cclient: TestClient) -> None:
    _save_keys(cclient)
    run(cclient, update_runtime_settings, _store(capp), monthly_budget_usd=0.1)
    r = _post(cclient, "/compare/check", {"job": "judge", **TWO_MODELS, "cap_usd": "0.25"})
    assert r.status_code == 422
    assert "$0.10 left this month" in r.text


# ---- running ------------------------------------------------------------------------------


def test_a_run_starts_in_the_background_and_changes_no_production_data(
    capp: FastAPI, cclient: TestClient, provider: Provider
) -> None:
    _save_keys(cclient)
    store = _store(capp)
    run(cclient, _items, store, 12)
    before = run(cclient, _snapshot, store)
    r = _post(cclient, "/compare/run", {"job": "judge", **TWO_MODELS, "cap_usd": "0.25"})
    assert r.status_code == 303, r.text
    match = re.fullmatch(r"/compare/runs/(\d+)", r.headers["location"])
    assert match is not None
    run_id = int(match.group(1))
    run(cclient, _drain, capp)
    record = run(cclient, store.compare_run, run_id)
    assert record is not None and record.status == "done"
    assert len(record.sample_ids) == 12
    assert len(provider.requests) == 4  # 2 models x 2 batches of the pipeline's 10
    assert not capp.state.run_lock.locked()
    assert run(cclient, _snapshot, store) == before
    spend = run(cclient, store._all, "SELECT provider, purpose FROM spend ORDER BY id")
    assert [tuple(s) for s in spend] == [("openrouter", "review")] * 2 + [("openai", "review")] * 2
    page = cclient.get(f"/compare/runs/{run_id}").text
    assert "google/gemini-3.8-flash" in page and "gpt-5.4-nano" in page
    assert "Lab item number 0" in page


def test_comparison_clients_never_retry(capp: FastAPI, cclient: TestClient, tmp_path: Path) -> None:
    """A retried request could be billed twice while the guard counts one worst case."""
    _save_keys(cclient)

    async def retries(app: FastAPI, models: list[CompareModel]) -> list[int]:
        async with app.state.compare_clients() as client_for:
            clients = [client_for(m.provider, m.model) for m in models]
            return [c._client.max_retries for c in clients]  # type: ignore[attr-defined]

    assert compare.MAX_RETRIES == 0
    assert run(cclient, retries, capp, [GEMINI, NANO]) == [0, 0]
    # The Anthropic SDK can't take the test transport: an app without one builds it.
    plain = create_app(
        make_settings(tmp_path / "plain"),
        ctx_factory=fake_ctx_factory,
        config_dir=CONFIG_DIR,
        clock=FixedClock(NOW),
        catalogue_fetch=fixture_catalogue,
    )
    with TestClient(plain, base_url=BASE_URL) as c:
        assert run(c, retries, plain, [HAIKU]) == [0]


def test_a_failed_run_releases_the_lock(
    capp: FastAPI, cclient: TestClient, provider: Provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save_keys(cclient)
    run(cclient, _items, _store(capp), 3)
    provider.hang = True
    monkeypatch.setattr(compare, "RUN_TIMEOUT_S", 0.2)
    r = _post(cclient, "/compare/run", {"job": "judge", **TWO_MODELS})
    assert r.status_code == 303
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    run(cclient, _drain, capp)
    record = run(cclient, _store(capp).compare_run, run_id)
    assert record is not None
    assert (record.status, record.error) == ("failed", compare.TOO_LONG)
    assert not capp.state.run_lock.locked()


def test_busy_when_the_pipeline_lock_is_held(
    capp: FastAPI, cclient: TestClient, provider: Provider
) -> None:
    _save_keys(cclient)
    run(cclient, _items, _store(capp), 3)
    lock: asyncio.Lock = capp.state.run_lock
    run(cclient, lock.acquire)
    try:
        r = _post(cclient, "/compare/run", {"job": "judge", **TWO_MODELS})
        assert r.status_code == 409
        assert "A run is in progress" in r.text
        assert run(cclient, _store(capp).compare_runs) == []
        assert provider.requests == []
    finally:
        lock.release()


def test_a_run_over_the_cap_is_refused(
    capp: FastAPI, cclient: TestClient, provider: Provider
) -> None:
    _save_keys(cclient)
    run(cclient, _items, _store(capp), 30)
    r = _post(cclient, "/compare/run", {"job": "judge", **TWO_MODELS, "cap_usd": "0.01"})
    assert r.status_code == 422
    assert "over the cap" in r.text
    assert run(cclient, _store(capp).compare_runs) == []
    assert provider.requests == []


def test_a_run_with_nothing_to_compare_is_refused(cclient: TestClient) -> None:
    _save_keys(cclient)
    r = _post(cclient, "/compare/run", {"job": "writer", **TWO_MODELS})
    assert r.status_code == 422
    assert "No stories to compare" in r.text


def test_a_running_comparison_polls_its_status(capp: FastAPI, cclient: TestClient) -> None:
    store = _store(capp)
    run_id = run(cclient, store.create_compare_run, "judge", [GEMINI, NANO], [1, 2], 0.25, NOW)
    page = cclient.get(f"/compare/runs/{run_id}").text
    assert f'hx-get="/compare/runs/{run_id}/status"' in page
    assert 'hx-trigger="every 3s"' in page
    fragment = cclient.get(f"/compare/runs/{run_id}/status", headers=HX)
    assert fragment.status_code == 200 and "HX-Refresh" not in fragment.headers
    assert "Running" in fragment.text and "<html" not in fragment.text
    run(cclient, store.finish_compare_run, run_id, "done", NOW, 0.0, None)
    done = cclient.get(f"/compare/runs/{run_id}/status", headers=HX)
    assert done.headers.get("HX-Refresh") == "true"
    assert 'hx-trigger="every 3s"' not in cclient.get(f"/compare/runs/{run_id}").text


def test_a_comparison_cut_off_by_a_restart_is_failed(capp: FastAPI, tmp_path: Path) -> None:
    with TestClient(capp, base_url=BASE_URL) as c:
        run_id = run(c, _store(capp).create_compare_run, "judge", [GEMINI, NANO], [], 0.25, NOW)
    again = create_app(
        make_settings(tmp_path),
        ctx_factory=fake_ctx_factory,
        config_dir=CONFIG_DIR,
        clock=FixedClock(NOW),
        catalogue_fetch=fixture_catalogue,
    )
    with TestClient(again, base_url=BASE_URL) as c:
        record = run(c, _store(again).compare_run, run_id)
    assert record is not None
    assert (record.status, record.error) == ("failed", compare.INTERRUPTED)


def test_an_unknown_run_is_404(cclient: TestClient) -> None:
    assert cclient.get("/compare/runs/999").status_code == 404


# ---- judge results ------------------------------------------------------------------------


def _judge_run(capp: FastAPI, cclient: TestClient, **kw: Any) -> tuple[int, list[int]]:
    store = _store(capp)
    ids = run(cclient, _items, store, 3, **kw)
    outputs: dict[str, list[dict[str, Any] | None]] = {
        GEMINI.key: [_verdict(ids[0], 8), _verdict(ids[1], 2), None],
        NANO.key: [_verdict(ids[0], 1), _verdict(ids[1], 2), _verdict(ids[2], 5)],
    }
    return run(cclient, _finished_run, store, "judge", [GEMINI, NANO], ids, outputs), ids


def test_judge_results_show_the_summary_and_highlight_disagreements(
    capp: FastAPI, cclient: TestClient
) -> None:
    run_id, ids = _judge_run(capp, cclient)
    page = cclient.get(f"/compare/runs/{run_id}").text
    assert 'id="compare-summary"' in page
    assert "Agreement with production" in page and "Accuracy against your labels" in page
    assert re.search(rf'<tr class="[^"]*disagree[^"]*" id="item-{ids[0]}"', page)
    assert not re.search(rf'<tr class="[^"]*disagree[^"]*" id="item-{ids[1]}"', page)
    assert "Failed" in page
    assert page.count('class="table-scroll"') >= 2  # wide tables scroll in their own box
    assert JUDGE_WARNING.replace("'", "&#39;") in page


def test_labels_need_csrf_and_update_the_accuracy(capp: FastAPI, cclient: TestClient) -> None:
    run_id, ids = _judge_run(capp, cclient)
    path = f"/compare/runs/{run_id}/label"
    no_token = cclient.post(path, data={"item_id": str(ids[0]), "label": "show"}, headers=HX)
    assert no_token.status_code == 403
    r = _post(cclient, path, {"item_id": str(ids[0]), "label": "important"}, headers=HX)
    assert r.status_code == 200
    assert "<html" not in r.text and 'id="judge-results"' in r.text
    assert re.search(r'value="clear"[^>]*aria-pressed="true"[^>]*>Important', r.text)
    assert run(cclient, _store(capp).labels, ids) == {ids[0]: "important"}
    # Gemini showed it (8), nano hid it (1): 100% and 0% against the one label
    summary = re.search(r'id="compare-summary".*?</table>', r.text, re.S)
    assert summary is not None and "100%" in summary.group(0) and "0%" in summary.group(0)
    plain = _post(cclient, path, {"item_id": str(ids[1]), "label": "junk"})
    assert plain.status_code == 303
    assert plain.headers["location"] == f"/compare/runs/{run_id}#item-{ids[1]}"
    cleared = _post(cclient, path, {"item_id": str(ids[0]), "label": "clear"}, headers=HX)
    assert cleared.status_code == 200
    assert run(cclient, _store(capp).labels, ids) == {ids[1]: "junk"}


def test_labels_only_for_items_in_the_run(capp: FastAPI, cclient: TestClient) -> None:
    run_id, ids = _judge_run(capp, cclient)
    path = f"/compare/runs/{run_id}/label"
    assert _post(cclient, path, {"item_id": "999", "label": "show"}).status_code == 404
    assert _post(cclient, path, {"item_id": str(ids[0]), "label": "meh"}).status_code == 422


def test_labels_are_reused_by_a_later_comparison(capp: FastAPI, cclient: TestClient) -> None:
    run_id, ids = _judge_run(capp, cclient)
    _post(cclient, f"/compare/runs/{run_id}/label", {"item_id": str(ids[0]), "label": "show"})
    store = _store(capp)
    later = run(
        cclient,
        _finished_run,
        store,
        "judge",
        [HAIKU, NANO],
        ids[:1],
        {HAIKU.key: [_verdict(ids[0], 9)], NANO.key: [_verdict(ids[0], 0)]},
    )
    page = cclient.get(f"/compare/runs/{later}").text
    assert re.search(r'value="clear"[^>]*aria-pressed="true"[^>]*>Show', page)
    summary = re.search(r'id="compare-summary".*?</table>', page, re.S)
    assert summary is not None and "100%" in summary.group(0)


def test_titles_and_links_are_escaped_and_safe(capp: FastAPI, cclient: TestClient) -> None:
    store = _store(capp)
    src = run(cclient, seed.add_source, store, name="Odd blog")
    item = run(
        cclient,
        seed.add_item,
        store,
        source_id=src,
        url="javascript:alert(1)",
        title="<b>Bold</b> claim",
        at=NOW,
        status=ItemStatus.QUEUED,
    )
    run_id = run(
        cclient,
        _finished_run,
        store,
        "judge",
        [GEMINI, NANO],
        [item],
        {GEMINI.key: [_verdict(item, 3)], NANO.key: [_verdict(item, 3)]},
    )
    page = cclient.get(f"/compare/runs/{run_id}").text
    assert "&lt;b&gt;Bold&lt;/b&gt; claim" in page and "<b>Bold</b>" not in page
    assert "javascript:" not in page
    assert 'rel="noopener noreferrer"' in page


# ---- writer results -----------------------------------------------------------------------


def _writer_run(
    capp: FastAPI, cclient: TestClient, summary: str = "Model summary."
) -> tuple[int, list[int]]:
    store = _store(capp)
    ids = run(cclient, _stories, store, 2)
    text: list[dict[str, Any] | None] = [
        StoryText(story_id=i, summary=f"{summary} {i}").model_dump(mode="json") for i in ids
    ]
    outputs: dict[str, list[dict[str, Any] | None]] = {GEMINI.key: text, NANO.key: [text[0], None]}
    return run(cclient, _finished_run, store, "writer", [GEMINI, NANO], ids, outputs), ids


def test_writer_results_side_by_side_with_preferences(capp: FastAPI, cclient: TestClient) -> None:
    run_id, ids = _writer_run(capp, cclient, summary="<script>alert(1)</script>")
    page = cclient.get(f"/compare/runs/{run_id}").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "<script>alert" not in page
    assert "Production summary 0." in page  # what Today shows now, for reference
    assert 'class="table-scroll"' in page
    path = f"/compare/runs/{run_id}/prefer"
    r = _post(cclient, path, {"story_id": str(ids[0]), "model_key": NANO.key}, headers=HX)
    assert r.status_code == 200 and 'id="writer-results"' in r.text and "<html" not in r.text
    assert run(cclient, _store(capp).writer_prefs, run_id) == {ids[0]: NANO.key}
    plain = _post(cclient, path, {"story_id": str(ids[1]), "model_key": GEMINI.key})
    assert plain.headers["location"] == f"/compare/runs/{run_id}#story-{ids[1]}"
    assert _post(cclient, path, {"story_id": str(ids[0]), "model_key": "x:y"}).status_code == 404
    assert _post(cclient, path, {"story_id": "999", "model_key": NANO.key}).status_code == 404
    page = cclient.get(f"/compare/runs/{run_id}").text
    tally = re.search(r'id="compare-summary".*?</table>', page, re.S)
    assert tally is not None
    assert "Preferred" in tally.group(0)


# ---- use this model -----------------------------------------------------------------------


def test_use_this_model_sets_the_judge_and_rebuilds(capp: FastAPI, cclient: TestClient) -> None:
    _save_keys(cclient)
    run_id, _ = _judge_run(capp, cclient)
    before = capp.state.ctx
    r = _post(cclient, f"/compare/runs/{run_id}/use", {"model_key": GEMINI.key})
    assert r.status_code == 303
    assert r.headers["location"] == f"/compare/runs/{run_id}?used=1"
    saved = run(cclient, load_runtime_settings, _store(capp))
    assert (saved.judge_provider, saved.judge_model) == ("openrouter", "google/gemini-3.8-flash")
    assert capp.state.ctx is not before
    page = cclient.get(r.headers["location"]).text
    assert "is now the judge" in page
    assert JUDGE_WARNING.replace("'", "&#39;") in page


def test_use_this_model_for_the_writer(capp: FastAPI, cclient: TestClient) -> None:
    _save_keys(cclient)
    run_id, _ = _writer_run(capp, cclient)
    r = _post(cclient, f"/compare/runs/{run_id}/use", {"model_key": NANO.key})
    assert r.status_code == 303
    saved = run(cclient, load_runtime_settings, _store(capp))
    assert (saved.writer_provider, saved.writer_model) == ("openai", "gpt-5.4-nano")
    assert (saved.judge_provider, saved.judge_model) == ("anthropic", "claude-haiku-5-5")


def test_use_this_model_refuses_a_value_the_settings_reject(
    capp: FastAPI, cclient: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save_keys(cclient)
    run_id, _ = _judge_run(capp, cclient)

    async def refuse(store: Store, **fields: Any) -> None:
        RuntimeSettings(judge_model="")  # raises ValidationError, as a refused save does

    monkeypatch.setattr(routes_compare, "update_runtime_settings", refuse)
    before = capp.state.ctx
    r = _post(cclient, f"/compare/runs/{run_id}/use", {"model_key": GEMINI.key})
    assert r.status_code == 422
    assert "This value was refused." in r.text
    assert capp.state.ctx is before


def test_use_this_model_is_validated_like_settings(capp: FastAPI, cclient: TestClient) -> None:
    run_id, _ = _judge_run(capp, cclient)  # no keys saved
    r = _post(cclient, f"/compare/runs/{run_id}/use", {"model_key": GEMINI.key})
    assert r.status_code == 422
    assert "Add your OpenRouter API key first." in r.text
    store = _store(capp)
    gone = CompareModel(provider="openrouter", model="nobody/unlisted")
    other = run(
        cclient, _finished_run, store, "judge", [gone, NANO], [], {gone.key: [], NANO.key: []}
    )
    _save_keys(cclient)
    r = _post(cclient, f"/compare/runs/{other}/use", {"model_key": gone.key})
    assert r.status_code == 422
    assert "OpenRouter model list" in r.text
    assert _post(cclient, f"/compare/runs/{other}/use", {"model_key": "a:b"}).status_code == 404
    saved = run(cclient, load_runtime_settings, store)
    assert (saved.judge_provider, saved.judge_model) == ("anthropic", "claude-haiku-5-5")


# ---- history ------------------------------------------------------------------------------


def test_history_lists_runs_newest_first_with_status_cost_and_best(
    capp: FastAPI, cclient: TestClient
) -> None:
    store = _store(capp)
    judge_id, _ = _judge_run(capp, cclient)
    writer_id, ids = _writer_run(capp, cclient)
    run(cclient, store.set_writer_pref, writer_id, ids[0], NANO.key, NOW)
    capped = run(
        cclient,
        _finished_run,
        store,
        "judge",
        [GEMINI, NANO],
        [],
        {GEMINI.key: [], NANO.key: []},
        status="capped",
    )
    page = cclient.get("/compare").text
    history = re.search(r'id="history".*', page, re.S)
    assert history is not None
    links = re.findall(r'href="/compare/runs/(\d+)"', history.group(0))
    assert [int(i) for i in links] == [capped, writer_id, judge_id]
    assert "Stopped at the cap" in history.group(0)
    assert "$0.00" in history.group(0) or "under $0.01" in history.group(0)
    # the writer's best is the most preferred; the judge's, without labels, production
    # agreement (both models hid item 1; gemini agrees on more)
    assert "gpt-5.4-nano" in history.group(0)
