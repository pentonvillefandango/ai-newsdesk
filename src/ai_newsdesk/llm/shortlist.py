"""The curated model shortlist (spec 3.3): config/models.yaml lists model ids per provider."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints

DEFAULT_MODELS_PATH = Path("config/models.yaml")

ModelId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _unique(ids: list[str]) -> list[str]:
    if len(ids) != len(set(ids)):
        raise ValueError("a model id is listed twice")
    return ids


IdList = Annotated[list[ModelId], AfterValidator(_unique)]


class Shortlist(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    anthropic: IdList
    openai: IdList
    openrouter: IdList

    @classmethod
    def load(cls, path: str | Path = DEFAULT_MODELS_PATH) -> Shortlist:
        return cls.model_validate(yaml.safe_load(Path(path).read_text()))

    def ids(self, provider: str) -> list[str]:
        """The curated ids for `provider`, in file order; empty for any other provider."""
        if provider in ("anthropic", "openai", "openrouter"):
            return list(getattr(self, provider))
        return []
