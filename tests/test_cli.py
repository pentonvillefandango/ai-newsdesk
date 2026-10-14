from __future__ import annotations

import io
from collections.abc import Iterator

import pytest
from argon2 import PasswordHasher

from ai_newsdesk import cli
from ai_newsdesk.cli import main


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _answers(monkeypatch: pytest.MonkeyPatch, *values: str) -> None:
    """Pretend to be a terminal, so hash-password prompts twice with getpass."""
    it: Iterator[str] = iter(values)
    monkeypatch.setattr("sys.stdin", _Tty())
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(it))


def test_hash_password_reads_one_line_when_stdin_is_not_a_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("a long enough password\n"))
    assert main(["hash-password"]) == 0
    captured = capsys.readouterr()
    [line] = captured.out.strip().splitlines()
    assert PasswordHasher().verify(line, "a long enough password")
    assert "a long enough password" not in captured.out + captured.err


def test_run_exits_with_mains_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["ai-newsdesk", "hash-password"])
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    with pytest.raises(SystemExit) as exit_info:
        cli.run()
    assert exit_info.value.code == 2


def test_hash_password_prints_a_verifiable_hash(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _answers(monkeypatch, "a long enough password", "a long enough password")
    assert main(["hash-password"]) == 0
    captured = capsys.readouterr()
    out = captured.out.strip()
    assert out.startswith("$argon2id$")
    assert PasswordHasher().verify(out, "a long enough password")
    assert "a long enough password" not in captured.out + captured.err


def test_hash_password_refuses_a_mismatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _answers(monkeypatch, "a long enough password", "a different password")
    assert main(["hash-password"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "don't match" in captured.err


def test_hash_password_refuses_a_short_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _answers(monkeypatch, "short", "short")
    assert main(["hash-password"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "at least 12" in captured.err
