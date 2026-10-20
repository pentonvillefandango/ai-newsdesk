import re

import pytest

from ai_newsdesk.narration import chunks, speech_text

LABEL = "Tuesday 20 October 2026"
HEAD = f"AI Newsdesk briefing for {LABEL}.\n\n"


@pytest.mark.parametrize(
    ("body", "want"),
    [
        ("Quiet day.", "Quiet day."),
        ("## Research\n\nA result.", "Research.\n\nA result."),
        ("## Research\nA result.", "Research.\n\nA result."),
        ("Intro.\n## Research\nA result.", "Intro.\n\nResearch.\n\nA result."),
        ("## Really?\n\nYes.", "Really?\n\nYes."),
        ("## Done.\n\nYes.", "Done.\n\nYes."),
        ("Sued in court [CTV News](https://x.example/a).", "Sued in court."),
        ("A launch [Verge](https://a.example) [Ars](https://b.example).", "A launch."),
        ("It shipped (see [Blog](https://a.example)).", "It shipped (see)."),
        ("[A](https://a.example) and [B](https://b.example) agreed.", "A and B agreed."),
        ("Ends with a source [Wired](https://w.example)", "Ends with a source"),
        (
            "Did it work [Wired](https://w.example)? Yes [Ars](https://a.example)!",
            "Did it work? Yes!",
        ),
        ("**Bold** and `code` at https://raw.example/x now.", "Bold and code at now."),
        ("One   two\nthree.\n\n\n\nFour.", "One two three.\n\nFour."),
        ("### Deep\n\nText.", "Deep.\n\nText."),
    ],
)
def test_speech_text(body: str, want: str) -> None:
    assert speech_text(body, LABEL) == HEAD + want


@pytest.mark.parametrize(
    "body",
    [
        f"{LABEL} brought news on chips.\n\nMore.",
        f"{LABEL}: a quiet day.",
        f"## {LABEL}\n\nA quiet day.",
    ],
)
def test_the_date_is_said_once_when_the_briefing_opens_with_it(body: str) -> None:
    # Live on 2026-10-20: "AI Newsdesk briefing for Tuesday 20 October 2026. Tuesday 20 October
    # 2026 brought news..." (feedback-4).
    spoken = speech_text(body, LABEL)
    assert spoken.startswith("AI Newsdesk briefing.\n\n")
    assert spoken.count(LABEL) == 1


def test_a_date_later_in_the_briefing_keeps_the_dated_intro() -> None:
    assert speech_text(f"Quiet day.\n\nAs of {LABEL}, nothing.", LABEL).startswith(HEAD)


def test_chunks_fit_in_one() -> None:
    assert chunks("short text") == ["short text"]


def test_chunks_split_at_paragraphs() -> None:
    para = "x" * 199 + "."
    text = "\n\n".join([para] * 100)  # about 20,000 characters
    got = chunks(text)
    assert len(got) == 3
    assert all(0 < len(c) <= 9000 for c in got)
    assert "\n\n".join(got) == text


def test_chunks_split_at_sentence_end() -> None:
    sentence = "word " * 19 + "end."
    text = " ".join([sentence] * 120)  # one paragraph of about 12,000 characters
    got = chunks(text)
    assert len(got) == 2
    assert all(c.endswith("end.") and len(c) <= 9000 for c in got)
    assert " ".join(got) == text


def test_chunks_hard_cut() -> None:
    text = "a" * 10000
    got = chunks(text)
    assert [len(c) for c in got] == [9000, 1000]
    assert "".join(got) == text


def test_chunks_keep_every_word_in_order() -> None:
    text = "\n\n".join(" ".join(f"w{i}-{j}" for j in range(400)) + "." for i in range(30))
    got = chunks(text, limit=2000)
    assert all(0 < len(c) <= 2000 for c in got)
    assert re.findall(r"w\d+-\d+", " ".join(got)) == re.findall(r"w\d+-\d+", text)
