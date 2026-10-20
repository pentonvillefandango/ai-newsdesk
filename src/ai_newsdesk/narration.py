"""Speech text for the briefing read aloud: pure functions, no I/O.

`speech_text` turns a stored briefing body (markdown-lite: `## ` headings, blank-line
paragraphs, `[text](url)` links) into plain text a voice can read. `chunks` splits that text
into pieces small enough for one text-to-speech request.
"""

from __future__ import annotations

import re

from ai_newsdesk.briefing import LINK_RE

# One or more links, with the whitespace before them, that end a sentence or a paragraph.
_CITATIONS = re.compile(rf"(?:\s*{LINK_RE.pattern})+(?=\s*(?:[.!?)]|$))", re.MULTILINE)
_BLANK_LINE = re.compile(r"\n[ \t]*\n")
_HEADING_LINE = re.compile(r"^[ \t]*(#+ [^\n]*)(?:\n|$)", re.MULTILINE)
_URL = re.compile(r"https?://[^\s<>]+")
_MARKUP = re.compile(r"[*_`]")
_SPACES = re.compile(r"\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_TRAILING_PUNCT = ".,;:!?)"


def _drop_url(match: re.Match[str]) -> str:
    url = match.group(0)
    kept = url[len(url.rstrip(_TRAILING_PUNCT)) :]
    return kept


def _speak_paragraph(paragraph: str) -> str:
    heading = paragraph.lstrip().startswith("#")
    text = _CITATIONS.sub("", paragraph)
    text = LINK_RE.sub(lambda m: m.group(1), text)
    text = _URL.sub(_drop_url, text)
    text = _MARKUP.sub("", text)
    text = _SPACES.sub(" ", text).strip().lstrip("#").strip()
    if heading and text and text[-1] not in ".!?":
        text += "."
    return text


def speech_text(body: str, day_label: str) -> str:
    paragraphs = [f"AI Newsdesk briefing for {day_label}."]
    # A heading line is its own paragraph even with no blank line around it.
    body = _HEADING_LINE.sub(r"\n\n\1\n\n", body.replace("\r\n", "\n"))
    for raw in _BLANK_LINE.split(body):
        spoken = _speak_paragraph(raw)
        if spoken:
            paragraphs.append(spoken)
    return "\n\n".join(paragraphs)


def _hard_cut(piece: str, limit: int) -> list[str]:
    """Cut a run with no sentence end into pieces of at most `limit`, at a space when possible."""
    out: list[str] = []
    while len(piece) > limit:
        cut = piece.rfind(" ", 1, limit + 1)
        if cut <= 0:
            cut = limit
        out.append(piece[:cut].strip())
        piece = piece[cut:].strip()
    if piece:
        out.append(piece)
    return [p for p in out if p]


def _pack(pieces: list[str], sep: str, limit: int) -> list[str]:
    out: list[str] = []
    current = ""
    for piece in pieces:
        joined = f"{current}{sep}{piece}" if current else piece
        if len(joined) <= limit:
            current = joined
            continue
        if current:
            out.append(current)
        current = piece
    if current:
        out.append(current)
    return out


def chunks(text: str, limit: int = 9000) -> list[str]:
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    for paragraph in (p.strip() for p in text.split("\n\n")):
        if not paragraph:
            continue
        if len(paragraph) <= limit:
            pieces.append(paragraph)
            continue
        sentences: list[str] = []
        for sentence in _SENTENCE_END.split(paragraph):
            sentences.extend(_hard_cut(sentence, limit))
        # Sentence-level packing must not be re-joined with paragraph pieces.
        pieces.extend(_pack(sentences, " ", limit))
    return _pack(pieces, "\n\n", limit)
