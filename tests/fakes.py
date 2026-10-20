"""In-memory fakes for every protocol, plus small builders, shared by all test files.

Each fake records its calls so tests can assert on what the code under test asked for.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from ai_newsdesk.models import (
    BriefingStory,
    Candidate,
    Category,
    JudgeResult,
    NarrationResult,
    Purpose,
    QuotaInfo,
    ReviewInput,
    SearchHit,
    SourceConfig,
    SourceKind,
    SourceResult,
    StoryText,
    Trust,
    Usage,
    Verdict,
    VoiceInfo,
    WriteInput,
)

FAKE_MODEL = "claude-haiku-5-5"


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """An aware UTC datetime, for terse test data."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def make_source_config(
    id: int = 1,
    kind: SourceKind = SourceKind.FEED,
    name: str = "Example Lab blog",
    config: dict[str, str | int | float | bool | list[str]] | None = None,
    trust: Trust = Trust.PRIMARY,
    enabled: bool = True,
    weight: float = 1.0,
) -> SourceConfig:
    return SourceConfig(
        id=id,
        kind=kind,
        name=name,
        config=config if config is not None else {"url": "https://lab.example.com/feed.xml"},
        trust=trust,
        enabled=enabled,
        weight=weight,
    )


def make_candidate(
    source_id: int = 1,
    url: str = "https://lab.example.com/news/model-x",
    title: str = "Example Lab releases Model X",
    author: str | None = None,
    published_at: datetime | None = None,
    snippet: str = "Model X is a new open-weights model.",
    hn_points: int | None = None,
) -> Candidate:
    return Candidate(
        source_id=source_id,
        url=url,
        title=title,
        author=author,
        published_at=published_at,
        snippet=snippet,
        hn_points=hn_points,
    )


def make_review_input(
    item_id: int = 1,
    title: str = "Example Lab releases Model X",
    text: str = "Model X is a new open-weights model.",
    source_name: str = "Example Lab blog",
    source_kind: SourceKind = SourceKind.FEED,
    trust: Trust = Trust.PRIMARY,
) -> ReviewInput:
    return ReviewInput(
        item_id=item_id,
        source_name=source_name,
        source_kind=source_kind,
        trust=trust,
        title=title,
        text=text,
    )


class FakeResearchEngine:
    """`ResearchEngine` fake. Unknown queries return []; unknown URLs fetch as None."""

    def __init__(
        self,
        search_results: dict[str, list[SearchHit]] | None = None,
        pages: dict[str, str] | None = None,
        healthy: bool = True,
        search_error: Exception | None = None,
    ) -> None:
        self.search_results = search_results or {}
        self.pages = pages or {}
        self.healthy = healthy
        self.search_error = search_error
        self.search_calls: list[tuple[str, int, str | None]] = []
        self.fetch_calls: list[str] = []

    async def search(
        self, query: str, *, max_results: int = 10, time_range: str | None = "day"
    ) -> list[SearchHit]:
        self.search_calls.append((query, max_results, time_range))
        if self.search_error is not None:
            raise self.search_error
        return list(self.search_results.get(query, []))[:max_results]

    async def fetch_markdown(self, url: str) -> str | None:
        self.fetch_calls.append(url)
        return self.pages.get(url)

    async def health(self) -> bool:
        return self.healthy


class FakeSource:
    """`Source` fake. Returns `results[src.id]` if given, else `candidates` re-homed to src.id."""

    def __init__(
        self,
        kind: SourceKind = SourceKind.FEED,
        candidates: list[Candidate] | None = None,
        results: dict[int, SourceResult] | None = None,
        error: str | None = None,
    ) -> None:
        self.kind = kind
        self.candidates = candidates or []
        self.results = results or {}
        self.error = error
        self.calls: list[tuple[int, datetime]] = []

    async def collect(self, src: SourceConfig, since: datetime) -> SourceResult:
        self.calls.append((src.id, since))
        if src.id in self.results:
            return self.results[src.id]
        if self.error is not None:
            return SourceResult(source_id=src.id, error=self.error)
        return SourceResult(
            source_id=src.id,
            candidates=[c.model_copy(update={"source_id": src.id}) for c in self.candidates],
        )


class FakeJudge:
    """`Judge` fake.

    Per input: an id in `fail_ids` fails; an id in `verdicts` gets that verdict; anything
    else gets `default_signal`, not junk, `default_category`, and `same_story_id` set to
    the first story candidate when `match_first_candidate` is true. Reports one `Usage`
    per call (100 input and 20 output tokens per item).
    """

    def __init__(
        self,
        model: str = FAKE_MODEL,
        verdicts: dict[int, Verdict] | None = None,
        fail_ids: set[int] | None = None,
        default_signal: int = 6,
        default_category: Category = Category.OTHER,
        match_first_candidate: bool = False,
        cost_per_item: float = 0.001,
    ) -> None:
        self.provider = "anthropic"
        self.model = model
        self.verdicts = verdicts or {}
        self.fail_ids = fail_ids or set()
        self.default_signal = default_signal
        self.default_category = default_category
        self.match_first_candidate = match_first_candidate
        self.cost_per_item = cost_per_item
        self.calls: list[list[ReviewInput]] = []

    def worst_case_cost(self, inputs: list[ReviewInput]) -> float:
        return self.cost_per_item * len(inputs)

    async def review(self, inputs: list[ReviewInput]) -> JudgeResult:
        self.calls.append(list(inputs))
        result = JudgeResult()
        for inp in inputs:
            if inp.item_id in self.fail_ids:
                result.failed_item_ids.append(inp.item_id)
            elif inp.item_id in self.verdicts:
                result.verdicts.append(self.verdicts[inp.item_id])
            else:
                same = (
                    inp.story_candidates[0].id
                    if self.match_first_candidate and inp.story_candidates
                    else None
                )
                result.verdicts.append(
                    Verdict(
                        item_id=inp.item_id,
                        signal=self.default_signal,
                        junk=False,
                        category=self.default_category,
                        same_story_id=same,
                    )
                )
        if inputs:
            result.usage.append(
                Usage(
                    provider="anthropic",
                    model=self.model,
                    purpose=Purpose.REVIEW,
                    input_tokens=100 * len(inputs),
                    output_tokens=20 * len(inputs),
                )
            )
        return result


class FakeWriter:
    """`Writer` fake.

    Stories get summary "Summary: <headline>" and, when `include_why`, why-it-matters
    "Why: <headline>". Stories in `fail_story_ids` are left out. The briefing body is
    `briefing_body` (None simulates a failure). One `Usage` per call that did work.
    """

    def __init__(
        self,
        model: str = FAKE_MODEL,
        fail_story_ids: set[int] | None = None,
        briefing_body: str | None = "Today's briefing.",
        cost_per_story: float = 0.001,
        briefing_cost: float = 0.002,
    ) -> None:
        self.provider = self.briefing_provider = "anthropic"
        self.model = self.briefing_model = model
        self.fail_story_ids = fail_story_ids or set()
        self.briefing_body = briefing_body
        self.cost_per_story = cost_per_story
        self.briefing_cost = briefing_cost
        self.story_calls: list[list[WriteInput]] = []
        self.briefing_calls: list[tuple[list[BriefingStory], str]] = []

    def worst_case_cost_stories(self, inputs: list[WriteInput]) -> float:
        return self.cost_per_story * len(inputs)

    def worst_case_cost_briefing(self, stories: list[BriefingStory]) -> float:
        return self.briefing_cost if stories else 0.0

    async def write_stories(self, inputs: list[WriteInput]) -> tuple[list[StoryText], list[Usage]]:
        self.story_calls.append(list(inputs))
        texts = [
            StoryText(
                story_id=inp.story_id,
                summary=f"Summary: {inp.headline}",
                why_it_matters=f"Why: {inp.headline}" if inp.include_why else None,
            )
            for inp in inputs
            if inp.story_id not in self.fail_story_ids
        ]
        usage = [
            Usage(
                provider="anthropic",
                model=self.model,
                purpose=Purpose.WRITE,
                input_tokens=200 * len(inputs),
                output_tokens=60 * len(inputs),
            )
        ]
        return texts, usage if inputs else []

    async def write_briefing(
        self, stories: list[BriefingStory], date_label: str
    ) -> tuple[str | None, list[Usage]]:
        self.briefing_calls.append((list(stories), date_label))
        if not stories:
            return None, []
        usage = Usage(
            provider="anthropic",
            model=self.model,
            purpose=Purpose.BRIEFING,
            input_tokens=150 * len(stories),
            output_tokens=800,
        )
        return self.briefing_body, [usage]


@dataclass
class NarrateCall:
    text_chunks: list[str]
    voice_id: str
    model_id: str
    credit_factor: float


class FakeNarrator:
    """A scripted Narrator: `result` for every narrate call (after the optional `during` hook,
    which lets a test change the world mid-call); `voices_result`/`factors_result` as given."""

    def __init__(
        self,
        result: NarrationResult | None = None,
        *,
        voices_result: list[VoiceInfo] | None = None,
        factors_result: dict[str, float] | None = None,
        during: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.result = result or NarrationResult(audio=b"ID3-fake-mp3", characters_billed=100)
        self.voices_result = voices_result
        self.factors_result = factors_result
        self.during = during
        self.calls: list[NarrateCall] = []

    async def narrate(
        self, text_chunks: list[str], *, voice_id: str, model_id: str, credit_factor: float
    ) -> NarrationResult:
        self.calls.append(NarrateCall(list(text_chunks), voice_id, model_id, credit_factor))
        if self.during is not None:
            await self.during()
        return self.result

    async def voices(self) -> list[VoiceInfo] | None:
        return self.voices_result

    async def model_factors(self) -> dict[str, float] | None:
        return self.factors_result

    async def quota(self) -> QuotaInfo | None:
        return None
