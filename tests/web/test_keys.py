from __future__ import annotations

from ai_newsdesk.web.keys import REDACTED, scrub


def test_scrub_removes_the_key_and_key_like_tokens() -> None:
    key = "sk-test-SECRET-123"  # gitleaks:allow
    assert (
        scrub(f"invalid x-api-key {key} rejected", key) == f"invalid x-api-key {REDACTED} rejected"
    )
    assert key not in scrub(f"{key}{key}", key)
    assert REDACTED in scrub("echoed sk-ant-api03-abcdefgh back", "other")
    assert REDACTED in scrub("token " + "A" * 40, "other")
    assert scrub("plain message", "sk-x") == "plain message"
    assert len(scrub("x" * 1000, "k")) <= 300
