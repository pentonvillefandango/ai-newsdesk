"""MP3 files for the read-aloud briefing, kept in one directory (feedback-3).

Plain synchronous file I/O: the files are small and writes are rare. Paths are built only from
a date and a generated name; `path()` refuses anything else.
"""

import datetime as dt
import os
import re
import secrets
from pathlib import Path

NAME_RE = re.compile(r"^briefing-\d{4}-\d{2}-\d{2}-[0-9a-f]{8}\.mp3$")


class AudioFiles:
    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)

    def write(self, day: dt.date, data: bytes) -> str:
        """Write `data` atomically (temp file, then rename) and return the new file name."""
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o750)
        name = f"briefing-{day.isoformat()}-{secrets.token_hex(4)}.mp3"
        final = self._dir / name
        tmp = self._dir / f"{name}.tmp"
        try:
            tmp.write_bytes(data)
            os.replace(tmp, final)
        finally:
            tmp.unlink(missing_ok=True)
        return name

    def path(self, name: str) -> Path:
        if not NAME_RE.fullmatch(name):
            raise ValueError("bad audio file name")
        return self._dir / name

    def delete(self, name: str) -> None:
        self.path(name).unlink(missing_ok=True)

    def names(self) -> set[str]:
        if not self._dir.is_dir():
            return set()
        return {p.name for p in self._dir.iterdir() if NAME_RE.fullmatch(p.name)}
