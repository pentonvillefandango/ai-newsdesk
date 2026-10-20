"""Settings "Read aloud": the section, its validation, the usage line, Refresh voices and the
voice-sample proxy (feedback-3)."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_newsdesk.models import QuotaInfo, VoiceInfo
from ai_newsdesk.runtime_settings import load_runtime_settings, update_runtime_settings
from ai_newsdesk.tts.catalogue import SETTING_KEY, CatalogueData
from ai_newsdesk.web.routes_settings import (
    SAMPLE_MAX_BYTES,
    SAMPLE_TIMEOUT_S,
)
from tests.web.support import NOW, csrf_token, run

GOOD_URL = "https://storage.googleapis.com/eleven-public-prod/premade/voices/abc/preview.mp3"
OWN_URL = "https://api.elevenlabs.io/v1/voices/own1/preview.mp3"
SECRET = "xi-secret-value-123"


def _voices() -> list[VoiceInfo]:
    return [
        VoiceInfo(
            voice_id="onwK4e9ZLuTAKqWW03F9",
            name="Daniel",
            labels={"accent": "british"},
            preview_url=GOOD_URL,
        ),
        VoiceInfo(
            voice_id="zed1",
            name="Zed",
            labels={"accent": "irish", "gender": "male"},
            preview_url=GOOD_URL,
        ),
        VoiceInfo(voice_id="amy1", name="Amy", labels={"gender": "female"}, preview_url=OWN_URL),
        VoiceInfo(voice_id="bare1", name="Bare", preview_url=None),
        VoiceInfo(
            voice_id="evil1", name="Evil1", preview_url="https://storage.googleapis.com/evil/x.mp3"
        ),
        VoiceInfo(
            voice_id="evil2", name="Evil2", preview_url="https://elevenlabs.io.evil.example/x.mp3"
        ),
        VoiceInfo(voice_id="evil3", name="Evil3", preview_url="http://api.elevenlabs.io/x.mp3"),
        VoiceInfo(voice_id="evil4", name="Evil4", preview_url="https://evil.example/x.mp3"),
        VoiceInfo(voice_id="evil5", name="Evil5", preview_url="https://notelevenlabs.io/x.mp3"),
        VoiceInfo(voice_id="ok2", name="Root", preview_url="https://elevenlabs.io/p.mp3"),
    ]


def _seed(client: TestClient, app: FastAPI, age: timedelta = timedelta(0)) -> None:
    data = CatalogueData(fetched_at=NOW - age, voices=_voices(), model_factors={"eleven_v4": 1.0})
    run(client, app.state.store.set_setting, SETTING_KEY, data.model_dump(mode="json"))
    run(client, app.state.deps.tts_catalogue.load)


def _key(client: TestClient, app: FastAPI) -> None:
    run(client, app.state.api_keys.set_api_key, "elevenlabs", SECRET, NOW)


def _mock(
    app: FastAPI, handler: httpx.MockTransport | None = None, **kw: object
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"ID3audio", headers={"content-type": "audio/mpeg"})

    app.state.http = httpx.AsyncClient(transport=handler or httpx.MockTransport(handle))
    return seen


FORM = {
    "runs_per_day": "4",
    "briefing_time": "07:00",
    "daily_review_cap": "500",
    "monthly_budget_usd": "5.00",
    "show_threshold": "4",
    "hn_min_points": "100",
    "narration_enabled": "on",
    "narration_voice_id": "onwK4e9ZLuTAKqWW03F9",
    "narration_model": "eleven_v4",
    "narration_monthly_chars": "300000",
}


def _post(client: TestClient, **over: str) -> Any:
    data = {**FORM, **over, "csrf_token": csrf_token(client)}
    return client.post("/settings", data=data, follow_redirects=False)


def _section(page: str) -> str:
    start = page.index('id="read-aloud"')
    return page[start : page.index("</section>", start)]


# ---- the section ------------------------------------------------------------------------------


def test_section_has_the_choices(app: FastAPI, client: TestClient) -> None:
    _seed(client, app)
    sec = _section(client.get("/settings").text)
    assert 'name="narration_enabled"' in sec and "checked" in sec
    assert '<optgroup label="Suggested">' in sec
    assert '<optgroup label="All voices on your account">' in sec
    suggested, rest = sec.split('<optgroup label="All voices on your account">')
    for vid in ("onwK4e9ZLuTAKqWW03F9", "pFZP5JQG7iQjIQuC4Bku", "XrExE9yKIg1WjnnlVkGX"):
        assert vid in suggested
    assert "Daniel" in suggested and "Lily" in suggested and "Matilda" in suggested
    assert "onwK4e9ZLuTAKqWW03F9" not in rest  # minus the suggested ids
    assert "Zed (irish, male)" in rest and "Amy (female)" in rest and ">Bare<" in rest
    assert rest.index("Amy") < rest.index("Zed")  # sorted by name
    for model, label in (
        ("eleven_v4", "Eleven v4 (best)"),
        ("eleven_multilingual_v2", "Multilingual v2 (most stable long-form)"),
        ("eleven_flash_v2_5", "Flash v2.5 (half the characters)"),
    ):
        assert f'value="{model}"' in sec and label in sec
    assert 'name="narration_monthly_chars"' in sec and 'value="300000"' in sec
    assert (
        "Play sample" in sec
        and "<audio" in sec
        and "voice-sample.js" in client.get("/settings").text
    )


def test_without_a_catalogue_only_suggestions(app: FastAPI, client: TestClient) -> None:
    sec = _section(client.get("/settings").text)
    assert '<optgroup label="Suggested">' in sec
    assert 'label="All voices on your account"' not in sec


def test_current_voice_outside_both_lists_is_selected_by_id(
    app: FastAPI, client: TestClient
) -> None:
    _seed(client, app)
    run(client, update_runtime_settings, app.state.store, narration_voice_id="mystery9")
    sec = _section(client.get("/settings").text)
    assert re.search(r'<option value="mystery9" selected>mystery9</option>', sec)


def test_current_voice_in_list_is_selected(app: FastAPI, client: TestClient) -> None:
    _seed(client, app)
    run(client, update_runtime_settings, app.state.store, narration_voice_id="zed1")
    sec = _section(client.get("/settings").text)
    assert re.search(r'<option value="zed1" selected>', sec)
    assert sec.count(" selected") == 2  # voice and model


# ---- saving -----------------------------------------------------------------------------------


def test_saving_stores_the_four_fields(app: FastAPI, client: TestClient) -> None:
    r = _post(
        client,
        narration_voice_id="zed1",
        narration_model="eleven_flash_v2_5",
        narration_monthly_chars="1234",
    )
    assert r.status_code == 303
    rs = run(client, load_runtime_settings, app.state.store)
    assert (rs.narration_enabled, rs.narration_voice_id) == (True, "zed1")
    assert (rs.narration_model, rs.narration_monthly_chars) == ("eleven_flash_v2_5", 1234)


def test_unticked_switch_turns_it_off(app: FastAPI, client: TestClient) -> None:
    data = {k: v for k, v in FORM.items() if k != "narration_enabled"}
    data["csrf_token"] = csrf_token(client)
    assert client.post("/settings", data=data, follow_redirects=False).status_code == 303
    assert run(client, load_runtime_settings, app.state.store).narration_enabled is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("narration_voice_id", "bad id!"),
        ("narration_voice_id", ""),
        ("narration_voice_id", "a" * 65),
        ("narration_model", "eleven_nope"),
        ("narration_monthly_chars", "-1"),
        ("narration_monthly_chars", "10000001"),
        ("narration_monthly_chars", "lots"),
    ],
)
def test_bad_values_show_inline_and_save_nothing(
    app: FastAPI, client: TestClient, field: str, value: str
) -> None:
    r = _post(client, **{field: value})
    assert r.status_code == 422
    assert f'id="{field}-error"' in r.text
    assert run(client, load_runtime_settings, app.state.store).narration_voice_id != "bad id!"


# ---- usage line -------------------------------------------------------------------------------


class _Narr:
    def __init__(self, info: QuotaInfo | None, boom: bool = False) -> None:
        self.info, self.boom, self.calls = info, boom, 0

    async def quota(self) -> QuotaInfo | None:
        self.calls += 1
        if self.boom:
            raise RuntimeError("x")
        return self.info


def test_usage_line_own_meter_only_without_a_key(app: FastAPI, client: TestClient) -> None:
    sec = _section(client.get("/settings").text)
    assert "This month: 0 of 300,000 characters used." in sec
    assert "ElevenLabs says" not in sec
    assert 'value="300000"' in sec  # the cap input stays a plain number


def test_usage_line_with_quota_cached_ten_minutes(app: FastAPI, client: TestClient) -> None:
    _key(client, app)
    narr = _Narr(QuotaInfo(tier="creator", character_count=100_000, character_limit=363_000))
    app.state.deps.narrator = narr
    sec = _section(client.get("/settings").text)
    assert "ElevenLabs says 263,000 of 363,000 left on your creator plan." in sec
    client.get("/settings")
    assert narr.calls == 1  # cached
    app.state.quota_cache = (NOW - timedelta(minutes=11), app.state.quota_cache[1])
    client.get("/settings")
    assert narr.calls == 2


def test_usage_line_has_thousands_separators_and_never_a_negative_left(
    app: FastAPI, client: TestClient
) -> None:
    _key(client, app)
    run(client, app.state.store.record_tts_usage, NOW, NOW.date(), "eleven_v4", 12_345, ok=True)
    app.state.deps.narrator = _Narr(
        QuotaInfo(tier="creator", character_count=370_000, character_limit=363_000)
    )
    sec = _section(client.get("/settings").text)
    assert "This month: 12,345 of 300,000 characters used." in sec
    assert "ElevenLabs says 0 of 363,000 left on your creator plan." in sec


@pytest.mark.parametrize("narr", [_Narr(None), _Narr(None, boom=True)])
def test_quota_failure_leaves_the_first_sentence(
    app: FastAPI, client: TestClient, narr: _Narr
) -> None:
    _key(client, app)
    app.state.deps.narrator = narr
    r = client.get("/settings")
    assert r.status_code == 200
    sec = _section(r.text)
    assert "characters used." in sec and "ElevenLabs says" not in sec


# ---- refresh ----------------------------------------------------------------------------------


def test_refresh_voices_saved_and_kept(app: FastAPI, client: TestClient) -> None:
    _key(client, app)
    for ok, word in ((True, "Voice list refreshed."), (False, "kept the old list")):

        async def refresh(ok: bool = ok) -> bool:
            return ok

        app.state.deps.tts_catalogue.refresh = refresh
        r = client.post(
            "/settings/voices/refresh",
            data={"csrf_token": csrf_token(client)},
            follow_redirects=False,
        )
        assert r.status_code == 303 and r.headers["location"].endswith("#read-aloud")
        page = client.get(r.headers["location"])
        assert word in page.text


def test_refresh_voices_needs_csrf_and_login(app: FastAPI, anon: TestClient) -> None:
    assert anon.post("/settings/voices/refresh", follow_redirects=False).status_code in (401, 403)


def test_opening_settings_refreshes_a_stale_copy_in_the_background(
    app: FastAPI, client: TestClient
) -> None:
    from ai_newsdesk import app as app_mod

    _key(client, app)
    _seed(client, app, age=timedelta(hours=30))
    calls: list[object] = []
    orig = app_mod.refresh_voices_in_background
    app_mod.refresh_voices_in_background = lambda ctx, bg: calls.append(ctx)  # type: ignore[assignment]
    try:
        client.get("/settings")
    finally:
        app_mod.refresh_voices_in_background = orig
    assert len(calls) == 1


def test_fresh_copy_or_no_key_does_not_refresh(app: FastAPI, client: TestClient) -> None:
    from ai_newsdesk import app as app_mod

    calls: list[object] = []
    orig = app_mod.refresh_voices_in_background
    app_mod.refresh_voices_in_background = lambda ctx, bg: calls.append(ctx)  # type: ignore[assignment]
    try:
        _seed(client, app, age=timedelta(hours=30))
        client.get("/settings")  # stale but no key
        _key(client, app)
        _seed(client, app)
        client.get("/settings")  # key but fresh
    finally:
        app_mod.refresh_voices_in_background = orig
    assert calls == []


# ---- the sample proxy -------------------------------------------------------------------------


def test_sample_needs_a_session(app: FastAPI, anon: TestClient) -> None:
    assert anon.get("/settings/voice-sample/zed1", follow_redirects=False).status_code in (303, 401)


def test_sample_streams_audio_with_cache_header(app: FastAPI, client: TestClient) -> None:
    _seed(client, app)
    seen = _mock(app)
    r = client.get("/settings/voice-sample/zed1")
    assert r.status_code == 200 and r.content == b"ID3audio"
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.headers["cache-control"] == "private, max-age=86400"
    assert str(seen[0].url) == GOOD_URL


def test_sample_content_type_falls_back_to_mpeg(app: FastAPI, client: TestClient) -> None:
    _seed(client, app)
    _mock(app, httpx.MockTransport(lambda req: httpx.Response(200, content=b"x")))
    assert client.get("/settings/voice-sample/zed1").headers["content-type"] == "audio/mpeg"


@pytest.mark.parametrize("head", [b"ID3\x04\x00", b"\xff\xfb\x90\x64", b"\xff\xf3\x40\xc4"])
@pytest.mark.parametrize("kind", ["text/plain", "application/octet-stream", "binary/octet-stream"])
def test_an_mp3_labelled_as_text_or_bytes_still_plays(
    app: FastAPI, client: TestClient, head: bytes, kind: str
) -> None:
    # Live on 2026-10-20 Google's bucket served Lily's and Matilda's previews as text/plain, so
    # every sample but Daniel's was a 502 (feedback-4).
    _seed(client, app)
    body = head + b"rest-of-mp3"
    _mock(
        app,
        httpx.MockTransport(
            lambda req: httpx.Response(200, content=body, headers={"content-type": kind})
        ),
    )
    r = client.get("/settings/voice-sample/zed1")
    assert r.status_code == 200 and r.content == body
    assert r.headers["content-type"] == "audio/mpeg"


@pytest.mark.parametrize(
    ("kind", "body"),
    [
        ("text/plain", b"<Error>NoSuchKey</Error>"),
        ("text/html", b"ID3 but html"),
        ("text/plain", b""),
    ],
)
def test_text_that_is_not_an_mp3_is_502(
    app: FastAPI, client: TestClient, kind: str, body: bytes
) -> None:
    _seed(client, app)
    _mock(
        app,
        httpx.MockTransport(
            lambda req: httpx.Response(200, content=body, headers={"content-type": kind})
        ),
    )
    assert client.get("/settings/voice-sample/zed1").status_code == 502


def test_sample_unknown_or_previewless_voice_is_404(app: FastAPI, client: TestClient) -> None:
    _seed(client, app)
    seen = _mock(app)
    assert client.get("/settings/voice-sample/nobody").status_code == 404
    assert client.get("/settings/voice-sample/bare1").status_code == 404
    assert seen == []


def test_sample_without_a_catalogue_is_404(app: FastAPI, client: TestClient) -> None:
    assert client.get("/settings/voice-sample/zed1").status_code == 404


@pytest.mark.parametrize("voice", ["evil1", "evil2", "evil3", "evil4", "evil5"])
def test_sample_rejects_off_allow_list_urls(app: FastAPI, client: TestClient, voice: str) -> None:
    _seed(client, app)
    _key(client, app)
    seen = _mock(app)
    assert client.get(f"/settings/voice-sample/{voice}").status_code == 404
    assert seen == []  # nothing was fetched, so no key was sent


def test_key_goes_only_to_elevenlabs_hosts(app: FastAPI, client: TestClient) -> None:
    _seed(client, app)
    _key(client, app)
    seen = _mock(app)
    assert client.get("/settings/voice-sample/zed1").status_code == 200  # storage.googleapis.com
    assert "xi-api-key" not in seen[-1].headers
    assert client.get("/settings/voice-sample/amy1").status_code == 200  # api.elevenlabs.io
    assert seen[-1].headers["xi-api-key"] == SECRET
    assert client.get("/settings/voice-sample/ok2").status_code == 200  # elevenlabs.io itself
    assert seen[-1].headers["xi-api-key"] == SECRET


def test_sample_never_follows_redirects(app: FastAPI, client: TestClient) -> None:
    _seed(client, app)
    seen: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.host == "evil.example":
            return httpx.Response(200, content=b"ID3evil", headers={"content-type": "audio/mpeg"})
        return httpx.Response(302, headers={"location": "https://evil.example/a"})

    _mock(app, httpx.MockTransport(handle))
    r = client.get("/settings/voice-sample/zed1")
    assert r.status_code == 502
    assert [str(req.url) for req in seen] == [GOOD_URL]  # one request; the target never asked


def test_sample_over_two_megabytes_is_502(app: FastAPI, client: TestClient) -> None:
    _seed(client, app)
    big = b"a" * (SAMPLE_MAX_BYTES + 1)
    _mock(app, httpx.MockTransport(lambda req: httpx.Response(200, content=big)))
    r = client.get("/settings/voice-sample/zed1")
    assert r.status_code == 502


def test_sample_exactly_at_cap_is_fine(app: FastAPI, client: TestClient) -> None:
    _seed(client, app)
    ok = b"a" * SAMPLE_MAX_BYTES
    _mock(app, httpx.MockTransport(lambda req: httpx.Response(200, content=ok)))
    assert client.get("/settings/voice-sample/zed1").status_code == 200


def test_sample_timeout_is_504(app: FastAPI, client: TestClient) -> None:
    _seed(client, app)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    _mock(app, httpx.MockTransport(boom))
    assert client.get("/settings/voice-sample/zed1").status_code == 504
    assert SAMPLE_TIMEOUT_S == 15.0


def test_sample_upstream_error_is_502_without_echo(app: FastAPI, client: TestClient) -> None:
    _seed(client, app)
    _key(client, app)
    _mock(
        app,
        httpx.MockTransport(lambda req: httpx.Response(401, content=f"bad {SECRET}".encode())),
    )
    r = client.get("/settings/voice-sample/amy1")
    assert r.status_code == 502 and SECRET not in r.text and "bad" not in r.text


def test_sample_connection_error_is_502(app: FastAPI, client: TestClient) -> None:
    _seed(client, app)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"no route {SECRET}", request=request)

    _mock(app, httpx.MockTransport(boom))
    r = client.get("/settings/voice-sample/zed1")
    assert r.status_code == 502 and SECRET not in r.text


def test_sample_non_audio_upstream_is_502(app: FastAPI, client: TestClient) -> None:
    _seed(client, app)
    _mock(
        app,
        httpx.MockTransport(
            lambda req: httpx.Response(
                200, content=b"<html>", headers={"content-type": "text/html"}
            )
        ),
    )
    assert client.get("/settings/voice-sample/zed1").status_code == 502
