"""APIFY_ACTOR entry point: on-demand lead scan billed per qualified lead (Pay-Per-Event).

Flow: read input -> scrape selected sources -> AI analysis (Groq) -> push every lead that
matches the user's filters to the dataset -> charge one `lead-analyzed` event per lead.

The scraping and AI layers are the same modules used by the Telegram SaaS mode.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from src import config
from src.ai_analyzer import AIAnalyzer
from src.models import AnalyzedLead
from src.scrapers import SCRAPER_CLASSES, scrape_all

logger = logging.getLogger("apify_actor")

CHARGE_EVENT_NAME = "lead-analyzed"
BATCH_SIZE = 12
MAX_ITEMS_LIMIT = 1000
MAX_AGE_HOURS_LIMIT = 168


class ActorConfigError(Exception):
    """Invalid input or missing server-side configuration (shown as the run's status)."""


class ActorLike(Protocol):
    """The subset of `apify.Actor` used here (lets tests inject a fake)."""

    async def push_data(self, data: Any) -> Any: ...
    async def charge(self, event_name: str, count: int = 1) -> Any: ...
    async def set_status_message(self, status_message: str, *, is_terminal: bool | None = None) -> Any: ...


@dataclass(slots=True)
class ActorInput:
    categories: tuple[str, ...] = config.CATEGORIES
    sources: tuple[str, ...] = tuple(SCRAPER_CLASSES)
    min_quality_score: int = 70
    max_items: int = 50
    max_age_hours: int = 24
    custom_keywords: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "ActorInput":
        data = data or {}

        categories = tuple(
            category
            for category in dict.fromkeys(str(c).upper() for c in _as_list(data.get("categories")))
            if category in config.CATEGORIES
        ) or config.CATEGORIES
        sources = tuple(
            source
            for source in dict.fromkeys(str(s).lower() for s in _as_list(data.get("sources")))
            if source in SCRAPER_CLASSES
        ) or tuple(SCRAPER_CLASSES)

        return cls(
            categories=categories,
            sources=sources,
            min_quality_score=_clamp_int(data.get("min_quality_score"), 70, 0, 100),
            max_items=_clamp_int(data.get("max_items"), 50, 1, MAX_ITEMS_LIMIT),
            max_age_hours=_clamp_int(data.get("max_age_hours"), 24, 1, MAX_AGE_HOURS_LIMIT),
            custom_keywords=[
                keyword.strip() for keyword in map(str, _as_list(data.get("custom_keywords"))) if keyword.strip()
            ],
        )


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def qualifies(lead: AnalyzedLead, options: ActorInput) -> bool:
    """A lead is delivered (and billed) only if it is a real client in a selected category."""
    return (
        lead.is_valid_lead
        and lead.category in options.categories
        and lead.quality_score >= options.min_quality_score
    )


async def run_scan(options: ActorInput, actor: ActorLike, analyzer: AIAnalyzer | None = None) -> int:
    """Scrape, analyse and deliver leads. Returns the number of leads pushed to the dataset."""
    await actor.set_status_message("Scanning Reddit, Hacker News, Freelancer.com and remote job boards...")
    raw_leads = await scrape_all(
        options.sources,
        max_age_minutes=options.max_age_hours * 60,
        custom_keywords=options.custom_keywords,
    )
    logger.info("%d candidate post(s) after pre-filtering", len(raw_leads))
    if not raw_leads:
        await actor.set_status_message("No matching posts found in the selected time window.", is_terminal=True)
        return 0

    own_analyzer = analyzer is None
    if analyzer is None:
        # Each Apify user sends their own applications, so the ready-made pitch stays on here.
        analyzer = AIAnalyzer(max_calls=min(max(options.max_items * 4, 40), 400), with_pitch=True)

    delivered = 0
    stop_reason = ""
    try:
        for start in range(0, len(raw_leads), BATCH_SIZE):
            await actor.set_status_message(
                f"Analysing posts {start + 1}-{min(start + BATCH_SIZE, len(raw_leads))} of {len(raw_leads)} "
                f"| leads delivered: {delivered}/{options.max_items}"
            )
            analyzed = await analyzer.analyze_many(raw_leads[start:start + BATCH_SIZE])

            for lead in analyzed:
                if not qualifies(lead, options):
                    continue
                await actor.push_data(lead.to_apify_dict())
                delivered += 1

                charge_result = await actor.charge(event_name=CHARGE_EVENT_NAME, count=1)
                if getattr(charge_result, "event_charge_limit_reached", False):
                    stop_reason = "the maximum cost per run was reached"
                    break
                if delivered >= options.max_items:
                    stop_reason = "the requested number of leads was reached"
                    break

            if stop_reason:
                break
            if analyzer.quota_exhausted:
                stop_reason = "the AI quota is exhausted"
                break
            if analyzer.calls_started >= analyzer.max_calls:
                stop_reason = "the AI call budget for this run was used"
                break
    finally:
        if own_analyzer:
            await analyzer.close()

    message = f"Delivered {delivered} qualified lead(s)"
    message += f" (stopped because {stop_reason})." if stop_reason else "."
    await actor.set_status_message(message, is_terminal=True)
    logger.info(message)
    return delivered


async def main() -> None:
    from apify import Actor  # imported lazily so the module stays importable without the SDK

    async with Actor:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

        options = ActorInput.from_dict(await Actor.get_input())
        problems = config.validate_config(config.MODE_APIFY_ACTOR)
        if problems:
            message = "Actor is not configured: " + " ".join(problems)
            await Actor.set_status_message(message, is_terminal=True)
            raise ActorConfigError(message)

        Actor.log.info(
            "Input: categories=%s sources=%s min_score=%d max_items=%d max_age_hours=%d keywords=%s",
            ",".join(options.categories), ",".join(options.sources), options.min_quality_score,
            options.max_items, options.max_age_hours, options.custom_keywords,
        )
        await run_scan(options, Actor)


if __name__ == "__main__":
    asyncio.run(main())
