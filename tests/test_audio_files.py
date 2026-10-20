import datetime as dt
import stat
from pathlib import Path

import pytest

from ai_newsdesk.audio_files import AudioFiles


def test_write_creates_dir_and_returns_name(tmp_path: Path) -> None:
    files = AudioFiles(tmp_path / "audio")
    name = files.write(dt.date(2026, 10, 20), b"mp3")
    assert name.startswith("briefing-2026-10-20-") and name.endswith(".mp3")
    assert files.path(name).read_bytes() == b"mp3"
    assert stat.S_IMODE((tmp_path / "audio").stat().st_mode) == 0o750
    assert files.names() == {name}
    assert [p.name for p in (tmp_path / "audio").iterdir()] == [name]  # no temp left


def test_names_are_unique(tmp_path: Path) -> None:
    files = AudioFiles(tmp_path)
    day = dt.date(2026, 10, 20)
    assert files.write(day, b"a") != files.write(day, b"b")


def test_delete_and_names(tmp_path: Path) -> None:
    files = AudioFiles(tmp_path / "audio")
    assert files.names() == set()
    name = files.write(dt.date(2026, 10, 20), b"x")
    files.delete(name)
    files.delete(name)  # missing is fine
    assert files.names() == set()


def test_names_ignores_foreign_files(tmp_path: Path) -> None:
    files = AudioFiles(tmp_path)
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "briefing-2026-10-20-abcd1234.mp3.tmp").write_text("x")
    assert files.names() == set()


@pytest.mark.parametrize(
    "bad",
    [
        "../briefing-2026-10-20-abcd1234.mp3",
        "briefing-2026-10-20-abcd1234.mp3/../x",
        "briefing-2026-10-20-ABCD1234.mp3",
        "briefing-2026-10-20-abcd123.mp3",
        "briefing-2026-10-20-abcd1234.mp3\n",
        "x.mp3",
        "",
    ],
)
def test_path_refuses_bad_names(tmp_path: Path, bad: str) -> None:
    files = AudioFiles(tmp_path)
    with pytest.raises(ValueError):
        files.path(bad)
    with pytest.raises(ValueError):
        files.delete(bad)
