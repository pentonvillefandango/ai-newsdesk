from __future__ import annotations

from ai_newsdesk.web.briefing_text import Block, Segment, parse_briefing

LAB = "https://lab.example/model"
PAPER = "https://paper.example/abs/1"


def test_headings_and_paragraphs() -> None:
    body = (
        "## Model releases\n\nFirst line\nsame paragraph.\n\n\n"
        "## Research\nA heading with text under it."
    )
    assert parse_briefing(body, set()) == [
        Block("heading", (Segment(text="Model releases"),)),
        Block("para", (Segment(text="First line same paragraph."),)),
        Block("heading", (Segment(text="Research"),)),
        Block("para", (Segment(text="A heading with text under it."),)),
    ]


def test_links_to_story_urls_become_link_segments() -> None:
    [block] = parse_briefing(
        f"Lab A [shipped a model]({LAB}) and a [paper]({PAPER}) followed.", {LAB, PAPER}
    )
    assert block.segments == (
        Segment(text="Lab A "),
        Segment(text="shipped a model", url=LAB),
        Segment(text=" and a "),
        Segment(text="paper", url=PAPER),
        Segment(text=" followed."),
    )


def test_other_links_become_plain_text() -> None:
    body = f"See [this](https://evil.example/x) and [that](javascript:void) and [ok]({LAB})."
    [block] = parse_briefing(body, {LAB})
    assert block.segments == (
        Segment(text="See this and that and "),
        Segment(text="ok", url=LAB),
        Segment(text="."),
    )


def test_unsafe_urls_never_link_even_if_listed() -> None:
    [block] = parse_briefing("[x](javascript:void)", {"javascript:void"})
    assert block.segments == (Segment(text="x"),)


def test_links_in_headings_and_html_stay_text() -> None:
    blocks = parse_briefing(f"## Policy [news]({LAB})\r\n\r\n<b>raw</b>", {LAB})
    assert blocks == [
        Block("heading", (Segment(text="Policy news"),)),
        Block("para", (Segment(text="<b>raw</b>"),)),
    ]


def test_empty_body() -> None:
    assert parse_briefing("  \n\n ", set()) == []


EVIL = "https://evil.example"
HOSTILE = [
    f"[]({EVIL})",
    f'[t]({EVIL} "x")',
    f"[t]( {EVIL})",
    f"[a\nb]({EVIL})",
    f"[a\\]]({EVIL})",
    f"[t][1]\n\n[1]: {EVIL}",
    EVIL,
    f"<{EVIL}>",
    f"[t](<{EVIL}>)",
    f"[t]({EVIL}\n)",
]


def test_hostile_link_forms_never_link_to_a_non_story_url() -> None:
    for body in HOSTILE:
        for block in parse_briefing(body, {LAB}):
            for seg in block.segments:
                assert seg.url in (None, LAB), body
