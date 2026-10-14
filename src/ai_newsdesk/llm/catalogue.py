"""The OpenRouter model catalogue (spec 3.2): which models are usable and what they cost.

The public models list (no key) is filtered to models with a positive numeric price, structured
output and text input, minus `:free` ids, and stored in the settings table under
`model_catalogue`. A failed refresh never replaces the last good copy.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from ai_newsdesk.pricing import CACHE_WRITE_MULTIPLIER
from ai_newsdesk.protocols import Clock
from ai_newsdesk.store.store import Store

log = logging.getLogger(__name__)

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
SETTING_KEY = "model_catalogue"
STALE_AFTER = timedelta(hours=24)
_PER_MTOK = 1_000_000
REQUIRED_PARAMETERS = ("structured_outputs", "response_format", "max_tokens")

CatalogueFetch = Callable[[], Awaitable[object]]


class CatalogueModel(BaseModel):
    id: str
    name: str
    input_per_mtok: float
    output_per_mtok: float
    context_length: int | None = None


def _usd(value: object) -> float | None:
    """A per-token price string as a finite float, else None."""
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _highest(pricing: dict[str, Any], *keys: str) -> float | None:
    """The highest price among `keys` in the base pricing and every tiered override, or None
    unless the base `keys[0]` price is positive.

    Tiered prices (e.g. above 100k prompt tokens) cost more, and the worst case must cover them.
    """
    base = _usd(pricing.get(keys[0]))
    if base is None or base <= 0:
        return None
    return max(base, _highest_any(pricing, *keys) or 0.0)


def _highest_any(pricing: dict[str, Any], *keys: str) -> float | None:
    """The highest positive price among `keys` in any tier, or None when none is listed."""
    tiers = [pricing, *[o for o in pricing.get("overrides") or [] if isinstance(o, dict)]]
    values = [v for t in tiers for k in keys if (v := _usd(t.get(k))) is not None and v > 0]
    return max(values) if values else None


def _entry(raw: object) -> CatalogueModel | None:
    if not isinstance(raw, dict):
        return None
    model_id = raw.get("id")
    if not isinstance(model_id, str) or not model_id or model_id.endswith(":free"):
        return None
    params = raw.get("supported_parameters")
    # We always send response_format and max_tokens, and OpenRouter calls set
    # require_parameters, so an endpoint must list all three.
    if not isinstance(params, list) or not set(REQUIRED_PARAMETERS) <= set(params):
        return None
    arch = raw.get("architecture")
    inputs = arch.get("input_modalities") if isinstance(arch, dict) else None
    if not isinstance(inputs, list) or "text" not in inputs:
        return None
    pricing = raw.get("pricing")
    if not isinstance(pricing, dict):
        return None
    prompt = _highest(pricing, "prompt")
    completion = _highest(pricing, "completion", "internal_reasoning")
    if prompt is None or completion is None:
        return None
    # The worst case prices input as a cache write at 1.25x the input price; raise the stored
    # input price when a model's cache write costs more than that.
    cache_write = _highest_any(pricing, "input_cache_write", "input_cache_write_1h")
    if cache_write is not None:
        prompt = max(prompt, cache_write / CACHE_WRITE_MULTIPLIER)
    name = raw.get("name")
    context = raw.get("context_length")
    return CatalogueModel(
        id=model_id,
        name=name if isinstance(name, str) and name else model_id,
        input_per_mtok=round(prompt * _PER_MTOK, 9),
        output_per_mtok=round(completion * _PER_MTOK, 9),
        context_length=context if isinstance(context, int) and context > 0 else None,
    )


def filter_models(payload: object) -> list[CatalogueModel]:
    """The usable models in an OpenRouter /models response. A malformed payload raises
    ValueError; malformed entries are skipped."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError("not an OpenRouter models list")
    kept = [m for m in (_entry(raw) for raw in data) if m is not None]
    return sorted(kept, key=lambda m: m.id)


@dataclass(frozen=True)
class Catalogue:
    models: dict[str, CatalogueModel] = field(default_factory=dict)
    fetched_at: datetime | None = None

    @classmethod
    def build(cls, models: Iterable[CatalogueModel], *, fetched_at: datetime) -> Catalogue:
        return cls(models={m.id: m for m in models}, fetched_at=fetched_at)

    def get(self, model_id: str) -> CatalogueModel | None:
        return self.models.get(model_id)

    def ids(self) -> list[str]:
        return sorted(self.models)

    def is_stale(self, now: datetime) -> bool:
        return self.fetched_at is None or now - self.fetched_at >= STALE_AFTER

    def to_json(self) -> dict[str, Any]:
        return {
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "models": [self.models[i].model_dump() for i in self.ids()],
        }

    @classmethod
    def from_json(cls, value: object) -> Catalogue:
        """Raises ValueError (or ValidationError, a subclass) on anything malformed."""
        if not isinstance(value, dict) or not isinstance(value.get("models"), list):
            raise ValueError("not a stored catalogue")
        fetched_at = datetime.fromisoformat(str(value.get("fetched_at")))
        if fetched_at.tzinfo is None:
            raise ValueError("catalogue time is not timezone-aware")
        models = [CatalogueModel.model_validate(m) for m in value["models"]]
        return cls.build(models, fetched_at=fetched_at)


def http_fetcher(http: httpx.AsyncClient, url: str = OPENROUTER_MODELS_URL) -> CatalogueFetch:
    """Fetch the public models list; raises on any HTTP or JSON error."""

    async def fetch() -> object:
        response = await http.get(url, headers={"Accept": "application/json"})
        response.raise_for_status()
        return response.json()

    return fetch


class ModelCatalogue:
    """The current catalogue, its persistence and its refresh."""

    def __init__(self, store: Store, fetch: CatalogueFetch, clock: Clock) -> None:
        self.store = store
        self._fetch = fetch
        self.clock = clock
        self.current: Catalogue | None = None

    async def load(self) -> Catalogue | None:
        """Read the stored catalogue (a corrupt one is ignored)."""
        raw = await self.store.get_setting(SETTING_KEY)
        if raw is None:
            return None
        try:
            self.current = Catalogue.from_json(raw)
        except (ValueError, ValidationError):
            log.warning("stored model catalogue is unreadable; ignored")
            return None
        return self.current

    def is_stale(self) -> bool:
        return self.current is None or self.current.is_stale(self.clock.now())

    async def refresh(self) -> bool:
        """Fetch, filter and store. Never raises: on any failure the last good copy stays."""
        try:
            models = filter_models(await self._fetch())
            if not models:
                raise ValueError("no usable models")
            catalogue = Catalogue.build(models, fetched_at=self.clock.now())
            await self.store.set_setting(SETTING_KEY, catalogue.to_json())
        except Exception as exc:  # failures are local: keep what we had
            log.warning(
                "model catalogue refresh failed (%s); kept the last good copy", type(exc).__name__
            )
            return False
        self.current = catalogue
        log.info("model catalogue refreshed: %d usable models", len(models))
        return True

    async def refresh_if_stale(self) -> bool:
        """Refresh when older than 24 h (or missing); True only if a refresh succeeded."""
        if not self.is_stale():
            return False
        return await self.refresh()
