# src/ai_newsdesk/judges/jev.py
"""Optional TypeSafe Jev judge (judge_provider = "typesafe"). Needs the `jev` extra.

Uses the bake-off's best question set (V3). Jev has no same-story question, so verdicts never
carry same_story_id; clustering then relies on the dedupe link and the title shortlist.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

from ai_newsdesk.claude_common import estimate_tokens, worst_case_usd
from ai_newsdesk.models import Category, JudgeResult, Purpose, ReviewInput, Usage, Verdict
from ai_newsdesk.pricing import Pricing

log = logging.getLogger(__name__)

JEV_MODEL = "jev-1.13.0"
QUESTION_BYTES = 12000  # upper bound on the question text sent per item (UTF-8 bytes = tokens)
NO_TEXT = "(no text, title only)"
CATEGORIES = [c for c in Category if c is not Category.OTHER]
CAT_TEXT = {
    "model_release": "a new or updated AI model being released or announced",
    "research": "an AI research paper, result or technical finding",
    "product": "an AI product, feature, tool or open-source release",
    "policy": "AI regulation, government policy, safety policy or legal action",
    "industry": "AI business news: funding, deals, hiring, company moves, compute",
    "opinion": "commentary, analysis or opinion about AI",
}
TRUST_NOTE = (
    "`source.trust` is `primary` when the organisation is publishing about its own work, "
    "`trusted` for a respected individual or their newsletter, and `discovery` for "
    "aggregators, searches and unknown accounts."
)


def build_questions() -> dict[str, Any]:
    from typesafe_sdk import Noul, Score  # pyright: ignore[reportMissingImports]

    questions: dict[str, Any] = {
        "ai_related": Noul(
            instructions="Is this item about artificial intelligence: AI models, research, "
            "products, policy or the AI industry?"
        ),
        "bait": Noul(
            instructions=[
                "Judge the item's content, not its formatting or tone.",
                TRUST_NOTE,
                "Is this item low-effort content that adds nothing a reader could not get "
                "from the original source?",
            ],
            criteria={
                "true": [
                    "Engagement bait or sensational hype without facts.",
                    "An SEO or tracker page that only aggregates other people's news.",
                    "An account excitedly reposting or summarising someone else's announcement.",
                    "A listicle, giveaway or self-promotional thread with no information.",
                ],
                "false": [
                    "A first-party announcement, release note, product or model launch by the "
                    "organisation or person who did the work.",
                    "A newsletter issue, podcast episode, essay or analysis by a known author, "
                    "even if the title is catchy.",
                    "Original reporting, a technical write-up, a research paper or a "
                    "substantive discussion.",
                ],
            },
        ),
        "importance": Score(
            instructions=[
                "How much does this item matter to someone who follows AI closely and wants "
                "to know what actually happened?",
                "Judge what the item reports, not how much text is supplied.",
            ],
            criteria=[
                "Not news: chatter, a joke, a personal aside or generic commentary.",
                "Minor: a small update, niche tool or personal take few people need.",
                "Useful: a solid write-up, a notable release in a niche, or worthwhile analysis.",
                "Important: a significant release, result, policy or industry event most "
                "followers should know about.",
                "Major: a landmark frontier model launch or event that dominates the AI news "
                "cycle.",
            ],
        ),
        "substance": Score(
            instructions="How much concrete technical or factual detail does the item contain?",
            criteria=[
                "None: a vague claim, emotion or slogan.",
                "Little: a headline-level claim with almost no supporting detail.",
                "Some: a few specifics such as names, numbers or features.",
                "Substantial: clear specifics such as benchmarks, methods, capabilities or "
                "limitations.",
                "Deep: rich technical or factual detail an expert would value.",
            ],
        ),
        "credibility": Score(
            instructions=[
                "How well do the source and the evidence in the item support the claims it makes?",
                TRUST_NOTE,
            ],
            criteria=[
                "Not credible: anonymous hype, rumour or likely misinformation.",
                "Weak: unverified claims from a source with no clear standing.",
                "Plausible: a reasonable source, but claims are not directly supported.",
                "Credible: a reputable source or clear supporting evidence.",
                "Authoritative: the primary source itself, such as the lab or author "
                "announcing their own work.",
            ],
        ),
    }
    for c in CATEGORIES:
        questions[f"cat_{c.value}"] = Noul(
            instructions=f"Is this item mainly about {CAT_TEXT[c.value]}?"
        )
    return questions


def state_for(inp: ReviewInput) -> dict[str, Any]:
    return {
        "source": {
            "name": inp.source_name,
            "kind": inp.source_kind.value,
            "trust": inp.trust.value,
        },
        "title": inp.title,
        "text": inp.text if inp.text.strip() else NO_TEXT,
    }


def jev_signal(*, ai: float, bait: float, imp: float, sub: float, cred: float) -> float:
    base = 10 * (0.55 * imp + 0.20 * sub + 0.25 * cred) / 4
    return max(0.0, base * ai - 4 * bait)


def verdict_from_answers(item_id: int, answers: Mapping[str, Any]) -> Verdict:
    ai = float(answers["ai_related"].noul)
    bait = float(answers["bait"].noul)
    signal = jev_signal(
        ai=ai,
        bait=bait,
        imp=float(answers["importance"].score),
        sub=float(answers["substance"].score),
        cred=float(answers["credibility"].score),
    )
    cats = {c: float(answers[f"cat_{c.value}"].noul) for c in CATEGORIES}
    best = max(cats, key=lambda c: cats[c])
    return Verdict(
        item_id=item_id,
        signal=max(0, min(10, round(signal))),
        junk=bait > 0.5 or ai < 0.5,
        category=best if cats[best] > 0.5 else Category.OTHER,
        same_story_id=None,
    )


class JevJudge:
    provider = "typesafe"

    def __init__(
        self,
        api_key: str,
        pricing: Pricing,
        *,
        model: str = JEV_MODEL,
        concurrency: int = 8,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._api_key = api_key
        self._pricing = pricing
        self.model = model
        self.batch_size = 10
        self._concurrency = concurrency
        self._factory = client_factory or self._default_factory

    def _default_factory(self) -> Any:
        from typesafe_sdk import AsyncTypeSafeClient  # pyright: ignore[reportMissingImports]

        return AsyncTypeSafeClient(api_key=self._api_key, model=self.model)

    def worst_case_cost(self, inputs: list[ReviewInput]) -> float:
        tokens = sum(estimate_tokens(json.dumps(state_for(i))) + QUESTION_BYTES for i in inputs)
        return worst_case_usd(self._pricing, self.model, tokens, 0)

    async def review(self, inputs: list[ReviewInput]) -> JudgeResult:
        if not inputs:
            return JudgeResult()
        verdicts: list[Verdict] = []
        failed: list[int] = []
        tokens = 0
        try:
            questions = build_questions()
            sem = asyncio.Semaphore(self._concurrency)
            async with self._factory() as client:

                async def one(inp: ReviewInput) -> None:
                    nonlocal tokens
                    try:
                        async with sem:
                            r = await client.system_one(state=state_for(inp), questions=questions)
                        tokens += int(r.usage.input_tokens or 0)
                        verdicts.append(verdict_from_answers(inp.item_id, r.answers))
                    except Exception as exc:  # one item fails, not the batch
                        log.warning("jev item %d failed: %s", inp.item_id, type(exc).__name__)
                        failed.append(inp.item_id)

                await asyncio.gather(*(one(i) for i in inputs))
        except Exception as exc:  # SDK missing or client failed: review never raises
            log.warning("jev review failed: %s", type(exc).__name__)
        done = {v.item_id for v in verdicts}
        failed = [i.item_id for i in inputs if i.item_id not in done]
        usage = (
            [
                Usage(
                    provider="typesafe",
                    model=self.model,
                    purpose=Purpose.REVIEW,
                    input_tokens=tokens,
                )
            ]
            if tokens
            else []
        )
        return JudgeResult(verdicts=verdicts, failed_item_ids=failed, usage=usage)
