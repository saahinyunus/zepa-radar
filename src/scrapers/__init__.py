"""Scraper registry and the `scrape_all` entry point shared by both execution modes."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Iterable

from src import config
from src.models import RawLead
from src.scrapers.base_scraper import BaseScraper, build_http_client
from src.scrapers.freelancer_scraper import FreelancerScraper
from src.scrapers.hn_scraper import HackerNewsScraper
from src.scrapers.jobboards_scraper import JobBoardsScraper
from src.scrapers.reddit_scraper import RedditScraper

logger = logging.getLogger(__name__)

SCRAPER_CLASSES: dict[str, type[BaseScraper]] = {
    "reddit": RedditScraper,
    "hackernews": HackerNewsScraper,
    "jobboards": JobBoardsScraper,
    "freelancer": FreelancerScraper,
}


def _title_key(title: str) -> str:
    """Normalised title used to spot duplicate postings (letters and digits only)."""
    key = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
    return key if len(key) >= 25 else ""


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
    throttle_boards: bool = False,
    report: dict[str, Any] | None = None,
) -> list[RawLead]:
    """Run the selected scrapers concurrently and return deduplicated, pre-filtered leads.

    * `sources`: subset of SCRAPER_CLASSES keys (default: all).
    * `max_age_minutes`: freshness window (default: config.LEAD_MAX_AGE_MINUTES).
    * `apply_prefilter`: drop "for hire" posts and posts without hiring signals.
    * `custom_keywords`: keep only leads mentioning at least one keyword (Apify input).
    * `rotate_reddit`: fetch only a random subset of Reddit groups (scheduled runs).
    * `throttle_boards`: poll rate-limited job boards (Jobicy) at most hourly (scheduled runs).
    * `report`: optional dict filled with {scraper: {"leads": n, "http": {host:status: count}}}.

    The result is sorted newest first.
    """
    names = list(dict.fromkeys(sources)) if sources is not None else list(SCRAPER_CLASSES)
    skipped = [name for name in names if name in config.DISABLED_SOURCES]
    if skipped:
        logger.info("Sources switched off (DISABLED_SOURCES): %s", ", ".join(skipped))
        names = [name for name in names if name not in config.DISABLED_SOURCES]
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
            elif name == "jobboards":
                options["throttle"] = throttle_boards
            scrapers.append(SCRAPER_CLASSES[name](**options))
        batches = await asyncio.gather(*(scraper.scrape() for scraper in scrapers))

    if report is not None:
        for scraper, batch in zip(scrapers, batches):
            report[scraper.name] = {"leads": len(batch), "http": dict(scraper.http_stats)}

    collected: dict[str, RawLead] = {}
    seen_titles: set[str] = set()
    for batch in batches:
        for lead in batch:
            if lead.item_id in collected:
                continue
            # The same opportunity can be posted twice or cross-posted; keep the first copy only.
            title_key = _title_key(lead.title)
            if title_key and title_key in seen_titles:
                continue
            seen_titles.add(title_key)
            collected[lead.item_id] = lead

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
    "FreelancerScraper",
    "HackerNewsScraper",
    "JobBoardsScraper",
    "RedditScraper",
    "SCRAPER_CLASSES",
    "scrape_all",
]
