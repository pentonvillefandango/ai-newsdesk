"""The LLM judge: batched structured-output review of candidate items over any ModelClient
(ADR-0001; Claude Haiku is the default)."""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from ai_newsdesk.judges.prompts import JUDGE_SYSTEM, render_items
from ai_newsdesk.llm.base import ModelClient, describe_error
from ai_newsdesk.models import Category, JudgeResult, Purpose, ReviewInput, Usage, Verdict

log = logging.getLogger(__name__)

CategoryName = Literal[
    "model_release", "research", "product", "policy", "industry", "opinion", "other"
]


class ItemJudgement(BaseModel):
    item_id: int = Field(description="The id attribute of the <item> tag being judged.")
    signal: int = Field(
        description="0-10: how worth reading this is for an informed AI practitioner who wants "
        "high-signal news only. 0 = junk/hype/repost, 10 = landmark news."
    )
    junk: bool = Field(
        description="True for engagement bait, hype without new information, a listicle, a "
        "giveaway, a promotional thread, an aggregator page, or a repost/rehash."
    )
    category: CategoryName
    same_story_id: int | None = Field(
        description="Id from this item's story_candidates if it reports the same event, else null."
    )


class BatchJudgement(BaseModel):
    verdicts: list[ItemJudgement]


class LLMJudge:
    """The production judge over any ModelClient (Claude Haiku by default, ADR-0001)."""

    def __init__(
        self,
        client: ModelClient,
        *,
        batch_size: int = 10,
        max_tokens: int = 4000,
    ) -> None:
        self.client = client
        self.provider = client.provider
        self.model = client.model
        self.batch_size = batch_size
        self.max_tokens = max_tokens

    def _batches(self, inputs: list[ReviewInput]) -> list[list[ReviewInput]]:
        return [inputs[i : i + self.batch_size] for i in range(0, len(inputs), self.batch_size)]

    def worst_case_cost(self, inputs: list[ReviewInput]) -> float:
        return sum(
            self.client.worst_case_usd(
                system=JUDGE_SYSTEM, user=render_items(batch), max_tokens=self.max_tokens
            )
            for batch in self._batches(inputs)
        )

    async def review(self, inputs: list[ReviewInput]) -> JudgeResult:
        result = JudgeResult()
        for batch in self._batches(inputs):
            verdicts, failed, usage = await self._review_batch(batch)
            result.verdicts.extend(verdicts)
            result.failed_item_ids.extend(failed)
            result.usage.extend(usage)
        return result

    async def _review_batch(
        self, batch: list[ReviewInput]
    ) -> tuple[list[Verdict], list[int], list[Usage]]:
        ids = [i.item_id for i in batch]
        try:
            answer = await self.client.structured(
                system=JUDGE_SYSTEM,
                user=render_items(batch),
                schema=BatchJudgement,
                max_tokens=self.max_tokens,
                purpose=Purpose.REVIEW,
            )
        except Exception as exc:  # review() never raises; the batch fails
            log.warning("judge batch of %d failed: %s", len(ids), describe_error(exc))
            return [], ids, []

        usage = [answer.usage] if answer.usage is not None else []
        parsed = answer.parsed
        if answer.refused or parsed is None:
            log.warning("judge batch of %d unusable: %s", len(ids), answer.error)
            return [], ids, usage

        by_id = {i.item_id: i for i in batch}
        verdicts: list[Verdict] = []
        seen: set[int] = set()
        for j in parsed.verdicts:
            item = by_id.get(j.item_id)
            if item is None or j.item_id in seen:
                continue
            seen.add(j.item_id)
            offered = {s.id for s in item.story_candidates}
            verdicts.append(
                Verdict(
                    item_id=j.item_id,
                    signal=max(0, min(10, j.signal)),
                    junk=j.junk,
                    category=Category(j.category),
                    same_story_id=j.same_story_id if j.same_story_id in offered else None,
                )
            )
        failed = [i for i in ids if i not in seen]
        return verdicts, failed, usage
