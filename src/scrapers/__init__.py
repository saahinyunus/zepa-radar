"""Scraper registry and the `scrape_all` entry point shared by both execution modes."""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from src import config
from src.models import RawLead
from src.scrapers.base_scraper import BaseScraper, build_http_client
from src.scrapers.hn_scraper import HackerNewsScraper
from src.scrapers.jobboards_scraper import JobBoardsScraper
from src.scrapers.reddit_scraper import RedditScraper

logger = logging.getLogger(__name__)

SCRAPER_CLASSES: dict[str, type[BaseScraper]] = {
    "reddit": RedditScraper,
    "hackernews": HackerNewsScraper,
    "jobboards": JobBoardsScraper,
}


def _matches_keywords(lead: RawLead, keywords: list[str]) -> bool:
    haystack = f"{lead.title}\n{lead.body}".lower()
    return any(keyword in haystack for keyword in keywords)


async def scrape_all(
    sources: Iterable[str] | None = None,
    *,
    max_age_minutes: int | None = None,
    apply_prefilter: bool = True,
    custom_keywords: Iterable[str] | None = None,
    rotate_reddit: bool = False,
) -> list[RawLead]:
    """Run the selected scrapers concurrently and return deduplicated, pre-filtered leads.

    * `sources`: subset of SCRAPER_CLASSES keys (default: all).
    * `max_age_minutes`: freshness window (default: config.LEAD_MAX_AGE_MINUTES).
    * `apply_prefilter`: drop "for hire" posts and posts without hiring signals.
    * `custom_keywords`: keep only leads mentioning at least one keyword (Apify input).
    * `rotate_reddit`: fetch only a rotating subset of Reddit groups (scheduled runs).

    The result is sorted newest first.
    """
    names = list(dict.fromkeys(sources)) if sources is not None else list(SCRAPER_CLASSES)
    unknown = [name for name in names if name not in SCRAPER_CLASSES]
    if unknown:
        raise ValueError(f"Unknown scraper source(s): {', '.join(unknown)}. Valid: {', '.join(SCRAPER_CLASSES)}")

    keywords = [kw.strip().lower() for kw in (custom_keywords or []) if kw and kw.strip()]

    async with build_http_client() as client:
        scrapers: list[BaseScraper] = []
        for name in names:
            options: dict[str, object] = {"client": client, "max_age_minutes": max_age_minutes}
            if name == "reddit":
                options["rotate"] = rotate_reddit
            scrapers.append(SCRAPER_CLASSES[name](**options))
        batches = await asyncio.gather(*(scraper.scrape() for scraper in scrapers))

    collected: dict[str, RawLead] = {}
    for batch in batches:
        for lead in batch:
            collected.setdefault(lead.item_id, lead)

    leads = list(collected.values())
    total = len(leads)

    if apply_prefilter:
        leads = [
            lead
            for lead in leads
            if config.passes_prefilter(
                lead.title, lead.body, trusted_source=lead.source in config.TRUSTED_HIRING_SOURCES
            )
        ]
    after_prefilter = len(leads)

    if keywords:
        leads = [lead for lead in leads if _matches_keywords(lead, keywords)]

    leads.sort(key=lambda lead: lead.posted_at.timestamp() if lead.posted_at else 0.0, reverse=True)
    logger.info(
        "scrape_all: %d scraped, %d after pre-filter, %d after keyword filter",
        total, after_prefilter, len(leads),
    )
    return leads


__all__ = [
    "BaseScraper",
    "HackerNewsScraper",
    "JobBoardsScraper",
    "RedditScraper",
    "SCRAPER_CLASSES",
    "scrape_all",
]
