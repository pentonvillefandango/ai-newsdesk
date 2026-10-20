"""Shared pydantic models and enums: the V1 contract every step uses (V1-S0)."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Trust(StrEnum):
    PRIMARY = "primary"
    TRUSTED = "trusted"
    DISCOVERY = "discovery"


class SourceKind(StrEnum):
    FEED = "feed"
    X_PERSON = "x_person"
    SEARCH = "search"
    ARXIV = "arxiv"
    GITHUB = "github"
    HN = "hn"


class Category(StrEnum):
    MODEL_RELEASE = "model_release"
    RESEARCH = "research"
    PRODUCT = "product"
    POLICY = "policy"
    INDUSTRY = "industry"
    OPINION = "opinion"
    OTHER = "other"


class ItemStatus(StrEnum):
    NEW = "new"
    DROPPED = "dropped"
    QUEUED = "queued"
    REVIEWED = "reviewed"
    REVIEW_FAILED = "review_failed"
    EXPIRED = "expired"


class Purpose(StrEnum):
    REVIEW = "review"
    WRITE = "write"
    BRIEFING = "briefing"


class SourceConfig(BaseModel):
    id: int
    kind: SourceKind
    name: str
    # e.g. {"url": ...} / {"handle": ...} / {"query": ...}
    config: dict[str, str | int | float | bool | list[str]]
    trust: Trust
    enabled: bool = True
    weight: float = 1.0  # feedback weight, 0.3..2.0


class Candidate(BaseModel):
    source_id: int
    url: str
    title: str
    author: str | None = None
    published_at: datetime | None = None
    snippet: str = ""
    hn_points: int | None = None


class SourceResult(BaseModel):
    source_id: int
    candidates: list[Candidate] = Field(default_factory=list)
    error: str | None = None
    duration_s: float = 0.0


class SearchHit(BaseModel):
    url: str
    title: str
    snippet: str = ""


class StoryRef(BaseModel):
    id: int
    headline: str


class ReviewInput(BaseModel):
    item_id: int
    source_name: str
    source_kind: SourceKind
    trust: Trust
    title: str
    author: str | None = None
    text: str  # body excerpt, else snippet, else "(no text, title only)"
    story_candidates: list[StoryRef] = Field(default_factory=list)


class Verdict(BaseModel):
    item_id: int
    signal: int = Field(ge=0, le=10)
    junk: bool
    category: Category
    same_story_id: int | None = None


class ConsolidationStory(BaseModel):
    """An open story offered to the consolidation pass (feedback-1, A1)."""

    id: int
    headline: str
    category: Category
    score: float
    first_seen_at: datetime


class Usage(BaseModel):
    provider: str  # "anthropic" | "typesafe" | "openai" | "openrouter"
    model: str
    purpose: Purpose
    input_tokens: int  # excludes cached tokens (cache_read_tokens / cache_write_tokens)
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # What the provider says the call cost (OpenRouter's usage.cost); preferred over our price.
    reported_cost_usd: float | None = None


class JudgeResult(BaseModel):
    verdicts: list[Verdict] = Field(default_factory=list)
    failed_item_ids: list[int] = Field(default_factory=list)
    usage: list[Usage] = Field(default_factory=list)


class WriteInput(BaseModel):
    story_id: int
    headline: str
    texts: list[str]  # best 1-2 item texts ("title\n\ntext")
    include_why: bool


class StoryText(BaseModel):
    story_id: int
    summary: str
    why_it_matters: str | None = None


class BriefingStory(BaseModel):
    story_id: int
    headline: str
    category: Category
    summary: str
    url: str  # lead item's url


class Story(BaseModel):  # what the web layer renders
    id: int
    headline: str
    category: Category
    score: float
    summary: str | None
    why_it_matters: str | None
    first_seen_at: datetime
    last_updated_at: datetime
    feedback: int  # -1, 0, 1
    sources: list["StorySource"] = Field(default_factory=list)
    scored: bool = True  # False when no item in the story has a verdict yet ("Unscored")
    dismissed: bool = False  # the owner hid it from Today (feedback-1 dismiss)


class StorySource(BaseModel):
    item_id: int
    source_name: str
    source_kind: SourceKind
    url: str
    title: str
    published_at: datetime | None = None


Story.model_rebuild()  # resolve the forward reference to StorySource


class UnscoredItem(BaseModel):
    """An item with no verdict (review failed, or still queued); never hidden from the owner."""

    item_id: int
    source_name: str
    source_kind: SourceKind
    url: str
    title: str
    published_at: datetime | None = None
    status: ItemStatus


class XPost(BaseModel):
    """A post from an X account source, for the X page (never part of a Today story)."""

    item_id: int
    handle: str  # the post's author, else the source's configured handle (no @)
    source_name: str
    url: str
    title: str
    text: str  # the search snippet
    published_at: datetime | None = None
    fetched_at: datetime
    signal: int | None = None
    status: ItemStatus
    junk: bool | None = None  # None: not judged yet
    dismissed: bool = False  # the owner hid it from the X page (feedback-1 dismiss)


class SourceOverview(BaseModel):
    """One row of the Sources page: the source plus its health and volume."""

    source: SourceConfig
    last_ok_at: datetime | None = None
    last_error: str | None = None
    items_7d: int = 0
    stories_7d: int = 0
    items_total: int = 0


class TokenTotals(BaseModel):
    """Summed token counts of some spend rows."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class UsageStats(BaseModel):
    """The last few days of real activity, used for the Settings cost estimate."""

    days: int
    runs: int
    items_reviewed: int
    review_usd: float
    write_usd: float
    briefing_usd: float
    # Tokens by purpose, so the estimate can price them with each job's chosen model.
    review_tokens: TokenTotals = Field(default_factory=TokenTotals)
    write_tokens: TokenTotals = Field(default_factory=TokenTotals)
    briefing_tokens: TokenTotals = Field(default_factory=TokenTotals)


class NarrationResult(BaseModel):
    audio: bytes | None = None
    characters_billed: int = 0  # characters sent x the model's credit factor, rounded up
    maybe_billed: bool = False  # True when the outcome is unknown or chunks succeeded
    error: str | None = None  # short, scrubbed


class VoiceInfo(BaseModel):
    voice_id: str
    name: str
    category: str | None = None
    labels: dict[str, str] = Field(default_factory=dict[str, str])
    preview_url: str | None = None


class QuotaInfo(BaseModel):
    tier: str
    character_count: int
    character_limit: int
    resets_at: datetime | None = None


class TtsModelInfo(BaseModel):
    model_id: str
    credit_factor: float = 1.0
    max_chars: int | None = None
