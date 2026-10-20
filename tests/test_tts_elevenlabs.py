import json
import logging
from collections.abc import Callable

import httpx
import pytest

from ai_newsdesk.tts.elevenlabs import ElevenLabsNarrator

KEY = "sk_secretvalue123456"


async def _key() -> str:
    return KEY


async def _no_key() -> str:
    return ""


def _make(
    handler: Callable[[httpx.Request], httpx.Response], key=_key
) -> tuple[ElevenLabsNarrator, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)

    http = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return ElevenLabsNarrator(http, key), seen


async def _narrate(n: ElevenLabsNarrator, chunks: list[str], factor: float = 1.0):
    return await n.narrate(chunks, voice_id="abc123", model_id="m1", credit_factor=factor)


async def test_single_chunk_success() -> None:
    n, seen = _make(lambda r: httpx.Response(200, content=b"ID3a", headers={"request-id": "r1"}))
    res = await _narrate(n, ["hello"], 0.5)
    assert res.audio == b"ID3a"
    assert res.characters_billed == 3  # ceil(5 * 0.5)
    assert not res.maybe_billed and res.error is None
    req = seen[0]
    assert req.method == "POST"
    assert req.url.path == "/v1/text-to-speech/abc123"
    assert req.url.params["output_format"] == "mp3_44100_64"
    assert req.headers["xi-api-key"] == KEY
    assert req.headers["accept"] == "audio/mpeg"
    assert json.loads(req.content) == {"text": "hello", "model_id": "m1"}


async def test_chunks_stitched_with_request_ids() -> None:
    counter = iter(range(1, 10))

    def h(r: httpx.Request) -> httpx.Response:
        i = next(counter)
        return httpx.Response(200, content=f"a{i}".encode(), headers={"request-id": f"r{i}"})

    n, seen = _make(h)
    res = await _narrate(n, ["one", "two", "three", "four", "five"])
    assert res.audio == b"a1a2a3a4a5"
    assert res.characters_billed == 3 + 3 + 5 + 4 + 4
    bodies = [json.loads(r.content) for r in seen]
    assert "previous_request_ids" not in bodies[0]
    assert bodies[1]["previous_request_ids"] == ["r1"]
    assert bodies[3]["previous_request_ids"] == ["r1", "r2", "r3"]
    assert bodies[4]["previous_request_ids"] == ["r2", "r3", "r4"]
    assert "previous_text" not in bodies[1]


async def test_previous_text_when_no_request_id() -> None:
    n, seen = _make(lambda r: httpx.Response(200, content=b"x"))
    long = "y" * 800
    res = await _narrate(n, [long, "next"])
    assert res.audio == b"xx"
    body = json.loads(seen[1].content)
    assert body["previous_text"] == "y" * 500
    assert "previous_request_ids" not in body


@pytest.mark.parametrize("status", [401, 422, 429])
async def test_4xx_on_first_chunk_not_billed(status: int) -> None:
    n, _ = _make(lambda r: httpx.Response(status, text=f"leak {KEY}"))
    res = await _narrate(n, ["a", "b"])
    assert res.audio is None
    assert res.characters_billed == 0 and not res.maybe_billed
    assert res.error == f"ElevenLabs answered {status}."


async def test_5xx_on_chunk_k_maybe_billed() -> None:
    calls = iter([200, 200, 503])
    n, _ = _make(lambda r: httpx.Response(next(calls), content=b"x", headers={"request-id": "r"}))
    res = await _narrate(n, ["aa", "bbb", "cccc", "dddd"])
    assert res.audio is None
    assert res.maybe_billed and res.characters_billed == 9
    assert res.error == "ElevenLabs answered 503."


async def test_5xx_on_first_chunk_counts_it() -> None:
    n, _ = _make(lambda r: httpx.Response(500))
    res = await _narrate(n, ["abcd", "e"])
    assert res.maybe_billed and res.characters_billed == 4


@pytest.mark.parametrize("exc", [httpx.ReadTimeout, httpx.ConnectError])
async def test_timeout_and_transport_maybe_billed(exc: type[httpx.HTTPError]) -> None:
    state = {"n": 0}

    def h(r: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 2:
            raise exc(f"boom {KEY}")
        return httpx.Response(200, content=b"x", headers={"request-id": "r"})

    n, _ = _make(h)
    res = await _narrate(n, ["aa", "bbb", "c"])
    assert res.audio is None and res.maybe_billed
    assert res.characters_billed == 5
    assert res.error is not None and KEY not in res.error
    assert exc.__name__ in res.error


async def test_4xx_on_later_chunk_counts_previous() -> None:
    calls = iter([200, 422])
    n, _ = _make(lambda r: httpx.Response(next(calls), content=b"x"))
    res = await _narrate(n, ["aa", "bbbb"], 1.0)
    assert res.audio is None
    assert res.maybe_billed and res.characters_billed == 2
    assert res.error == "ElevenLabs answered 422."


async def test_empty_key_no_request() -> None:
    n, seen = _make(lambda r: httpx.Response(200), key=_no_key)
    res = await _narrate(n, ["a"])
    assert res.error == "No ElevenLabs key."
    assert res.audio is None and not seen


@pytest.mark.parametrize("bad", ["a/b", "", "a b", "../x", "x" * 65, "a?b"])
async def test_bad_voice_id_no_request(bad: str) -> None:
    n, seen = _make(lambda r: httpx.Response(200, content=b"x"))
    res = await n.narrate(["a"], voice_id=bad, model_id="m", credit_factor=1.0)
    assert res.error and res.audio is None and not seen


async def test_key_not_in_logs_or_error(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    n, _ = _make(lambda r: httpx.Response(401, text=KEY))
    res = await _narrate(n, ["a"])
    assert KEY not in (res.error or "")
    assert KEY not in caplog.text
    assert await n.quota() is None
    assert KEY not in caplog.text


async def test_voices_paginate_and_map() -> None:
    pages = {
        None: {
            "voices": [
                {
                    "voice_id": "v1",
                    "name": "Alice",
                    "category": "premade",
                    "labels": {"accent": "british", "n": 3},
                    "preview_url": "https://storage.googleapis.com/x.mp3",
                }
            ],
            "has_more": True,
            "next_page_token": "tok",
        },
        "tok": {
            "voices": [{"voice_id": "v2", "name": "Bob", "labels": None}],
            "has_more": False,
            "next_page_token": None,
        },
    }
    n, seen = _make(lambda r: httpx.Response(200, json=pages[r.url.params.get("next_page_token")]))
    voices = await n.voices()
    assert voices is not None
    assert [v.voice_id for v in voices] == ["v1", "v2"]
    assert voices[0].labels == {"accent": "british"}
    assert voices[0].category == "premade"
    assert voices[1].preview_url is None
    assert seen[0].url.path == "/v2/voices"
    assert seen[0].url.params["page_size"] == "100"
    assert seen[0].headers["xi-api-key"] == KEY


async def test_voices_error_is_none() -> None:
    n, _ = _make(lambda r: httpx.Response(500))
    assert await n.voices() is None
    n2, _ = _make(lambda r: httpx.Response(200, text="not json"))
    assert await n2.voices() is None


async def test_model_factors() -> None:
    body = [
        {
            "model_id": "a",
            "can_do_text_to_speech": True,
            "model_rates": {"character_cost_multiplier": 0.5},
        },
        {
            "model_id": "b",
            "can_do_text_to_speech": True,
            "model_rates": {"character_cost_multiplier": 1.0},
        },
        {
            "model_id": "c",
            "can_do_text_to_speech": False,
            "model_rates": {"character_cost_multiplier": 1.0},
        },
        {"model_id": "d", "can_do_text_to_speech": True},
    ]
    n, seen = _make(lambda r: httpx.Response(200, json=body))
    assert await n.model_factors() == {"a": 0.5, "b": 1.0, "d": 1.0}
    assert seen[0].url.path == "/v1/models"


async def test_model_factors_error() -> None:
    n, _ = _make(lambda r: httpx.Response(401))
    assert await n.model_factors() is None


async def test_quota() -> None:
    body = {
        "tier": "creator",
        "character_count": 100,
        "character_limit": 100000,
        "next_character_count_reset_unix": 1_800_000_000,
        "other": 1,
    }
    n, seen = _make(lambda r: httpx.Response(200, json=body))
    q = await n.quota()
    assert q is not None
    assert (q.tier, q.character_count, q.character_limit) == ("creator", 100, 100000)
    assert q.resets_at is not None and q.resets_at.tzinfo is not None
    assert int(q.resets_at.timestamp()) == 1_800_000_000
    assert seen[0].url.path == "/v1/user/subscription"


async def test_quota_error_and_no_key() -> None:
    n, _ = _make(lambda r: httpx.Response(500))
    assert await n.quota() is None
    n2, seen = _make(lambda r: httpx.Response(200), key=_no_key)
    assert await n2.quota() is None and not seen


async def test_decoding_error_is_maybe_billed() -> None:
    def h(r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not gzip", headers={"content-encoding": "gzip"})

    n, _ = _make(h)
    res = await _narrate(n, ["abc"])
    assert res.audio is None and res.maybe_billed and res.characters_billed == 3
    assert res.error is not None and KEY not in res.error


async def test_closed_client_does_not_raise() -> None:
    n, _ = _make(lambda r: httpx.Response(200, content=b"x"))
    await n._http.aclose()  # pyright: ignore[reportPrivateUsage]
    res = await _narrate(n, ["abc"])
    assert res.audio is None and res.error


@pytest.mark.parametrize("factor", [float("nan"), float("inf"), -1.0])
async def test_bad_credit_factor_no_request(factor: float) -> None:
    n, seen = _make(lambda r: httpx.Response(200, content=b"x"))
    res = await _narrate(n, ["abc"], factor)
    assert res.error and res.audio is None and not seen


async def test_empty_chunks_is_not_success() -> None:
    n, seen = _make(lambda r: httpx.Response(200, content=b"x"))
    res = await _narrate(n, [])
    assert res.error == "Nothing to narrate." and res.audio is None and not seen


async def test_quota_huge_reset_is_none_not_raise() -> None:
    body = {
        "tier": "t",
        "character_count": 1,
        "character_limit": 2,
        "next_character_count_reset_unix": 10**20,
    }
    n, _ = _make(lambda r: httpx.Response(200, json=body))
    q = await n.quota()
    assert q is not None and q.resets_at is None


async def test_redirect_not_followed_key_not_resent() -> None:
    def h(r: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/x"})

    n, seen = _make(h)
    res = await _narrate(n, ["a"])
    assert res.audio is None and res.error
    assert len(seen) == 1
    n2, seen2 = _make(h)
    assert await n2.quota() is None and len(seen2) == 1
    assert await n2.voices() is None and len(seen2) == 2


async def test_no_requests_after_failing_chunk() -> None:
    n, seen = _make(lambda r: httpx.Response(500))
    await _narrate(n, ["a", "b", "c"])
    assert len(seen) == 1


async def test_raising_key_resolver_gives_error() -> None:
    async def boom() -> str:
        raise RuntimeError(KEY)

    n, seen = _make(lambda r: httpx.Response(200), key=boom)
    res = await _narrate(n, ["a"])
    assert res.error and KEY not in res.error and not seen
