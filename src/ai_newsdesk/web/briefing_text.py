"""Turns a stored briefing body (step 3's "markdown-lite") into blocks for the template.

Format (master plan R4): plain text; a line starting "## " is a section heading; blank lines
separate paragraphs; links are [text](url). Nothing here produces HTML: the template renders
every piece with autoescape. A link survives only if its URL is one of the briefing's story
URLs (passed in) and is a plain http(s) URL; anything else becomes its text.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

from ai_newsdesk.web.deps import safe_url

# The same pattern as step 3's briefing.clean_briefing, so both sides agree on what a link is.
LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_BLANK_LINE = re.compile(r"\n[ \t]*\n")


@dataclass(frozen=True)
class Segment:
    text: str = ""
    url: str | None = None


@dataclass(frozen=True)
class Block:
    kind: Literal["heading", "para"]
    segments: tuple[Segment, ...]


def _segments(text: str, allowed: Collection[str]) -> tuple[Segment, ...]:
    """Split one flattened line into text and link segments; adjacent text is merged."""
    out: list[Segment] = []

    def add_text(piece: str) -> None:
        if not piece:
            return
        if out and out[-1].url is None:
            out[-1] = Segment(text=out[-1].text + piece)
        else:
            out.append(Segment(text=piece))

    pos = 0
    for match in LINK_RE.finditer(text):
        add_text(text[pos : match.start()])
        label, url = match.group(1), match.group(2)
        if url in allowed and safe_url(url) == url:
            out.append(Segment(text=label, url=url))
        else:
            add_text(label)
        pos = match.end()
    add_text(text[pos:])
    return tuple(out)


def _flat(lines: list[str]) -> str:
    return " ".join(" ".join(lines).split())


def parse_briefing(body: str, allowed_urls: Collection[str]) -> list[Block]:
    blocks: list[Block] = []
    for chunk in _BLANK_LINE.split(body.replace("\r\n", "\n").replace("\r", "\n").strip()):
        para: list[str] = []
        for line in chunk.split("\n"):
            if line.startswith("## "):
                if para:
                    blocks.append(Block("para", _segments(_flat(para), allowed_urls)))
                    para = []
                # Headings are plain text: a link inside one keeps only its label.
                heading = _flat([LINK_RE.sub(r"\1", line[3:])])
                if heading:
                    blocks.append(Block("heading", (Segment(text=heading),)))
            elif line.strip():
                para.append(line)
        if para:
            blocks.append(Block("para", _segments(_flat(para), allowed_urls)))
    return blocks
