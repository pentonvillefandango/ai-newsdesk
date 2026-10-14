"""Shared pieces for source adapters: typed config access, look-back cut-offs and never-raise
wrappers."""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta

from ai_newsdesk.models import Candidate, SourceConfig, SourceKind, SourceResult
from ai_newsdesk.protocols import Source

log = logging.getLogger(__name__)

USER_AGENT = "ai-newsdesk/1 (self-hosted news reader)"
ERROR_MAX_CHARS = 300


class SourceConfigError(ValueError):
    """A source's config is missing a key or has the wrong type."""


def cfg_str(src: SourceConfig, key: str, default: str | None = None) -> str:
    """A string config value. With no default, the key is required and must be non-empty."""
    value = src.config.get(key, default)
    if isinstance(value, str) and (value or default is not None):
        return value
    raise SourceConfigError(
        f"source {src.id} ({src.name}): config '{key}' must be a non-empty string"
    )


def cfg_int(src: SourceConfig, key: str, default: int) -> int:
    value = src.config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SourceConfigError(f"source {src.id} ({src.name}): config '{key}' must be an integer")
    return value


def cfg_list(src: SourceConfig, key: str, default: list[str]) -> list[str]:
    value = src.config.get(key, default)
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return list(value)
    raise SourceConfigError(
        f"source {src.id} ({src.name}): config '{key}' must be a list of strings"
    )


def cutoff(src: SourceConfig, since: datetime, default_lookback_hours: int) -> datetime:
    """Oldest publish time to keep. The look-back catches late-indexed items; dedupe drops
    repeats."""
    return since - timedelta(hours=cfg_int(src, "lookback_hours", default_lookback_hours))


def keep_after(published: datetime | None, cut: datetime) -> bool:
    """Undated entries are kept: the store's canonical-URL dedupe stops them repeating."""
    return published is None or published >= cut


def time_range_for(since: datetime, now: datetime) -> str:
    """research-engine `time_range` covering the interval since the last run."""
    gap = now - since
    if gap <= timedelta(days=1):
        return "day"
    if gap <= timedelta(days=7):
        return "week"
    if gap <= timedelta(days=31):
        return "month"
    return "year"


def error_text(exc: BaseException) -> str:
    message = str(exc).strip()
    text = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return text[:ERROR_MAX_CHARS]


class BaseSource(ABC):
    """Adapters implement `fetch_candidates`, which may raise; `collect` times it and never
    raises."""

    kind: SourceKind

    async def collect(self, src: SourceConfig, since: datetime) -> SourceResult:
        start = time.perf_counter()
        try:
            candidates = await self.fetch_candidates(src, since)
        except Exception as exc:
            log.warning("source %s (%s) failed", src.id, src.name, exc_info=True)
            return SourceResult(
                source_id=src.id, error=error_text(exc), duration_s=time.perf_counter() - start
            )
        return SourceResult(
            source_id=src.id, candidates=candidates, duration_s=time.perf_counter() - start
        )

    @abstractmethod
    async def fetch_candidates(self, src: SourceConfig, since: datetime) -> list[Candidate]: ...


async def safe_collect(
    source: Source, src: SourceConfig, since: datetime, *, timeout_s: float = 60.0
) -> SourceResult:
    """Run any Source with a timeout. Never raises: failures come back in `SourceResult.error`."""
    start = time.perf_counter()
    try:
        async with asyncio.timeout(timeout_s):
            result = await source.collect(src, since)
    except TimeoutError:
        return SourceResult(
            source_id=src.id,
            error=f"timed out after {timeout_s:g}s",
            duration_s=time.perf_counter() - start,
        )
    except Exception as exc:
        log.warning("source %s (%s) raised from collect", src.id, src.name, exc_info=True)
        return SourceResult(
            source_id=src.id, error=error_text(exc), duration_s=time.perf_counter() - start
        )
    if result.duration_s == 0.0:
        result = result.model_copy(update={"duration_s": time.perf_counter() - start})
    return result
