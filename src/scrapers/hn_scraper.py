"""Hacker News scraper using the public Algolia `search_by_date` API."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from src import config
from src.models import RawLead, make_item_id, parse_timestamp
from src.scrapers.base_scraper import BaseScraper, guess_category, html_to_text

logger = logging.getLogger(__name__)

TITLE_MAX_CHARS = 140
# Threads where people advertise themselves instead of hiring.
SELF_PROMO_THREAD_MARKERS = ("wants to be hired",)
# "Freelancer? Seeking freelancer?" mixes both sides: comments open with "SEEKING FREELANCER"
# (a client, keep) or "SEEKING WORK" (a freelancer advertising, drop).
SELF_PROMO_COMMENT_STARTS = ("seeking work", "available for hire", "for hire")
# Comments are only hiring posts inside these monthly threads; elsewhere a "looking for" search
# hit is ordinary conversation. Stories (e.g. "Ask HN: ... hiring") are kept and judged by the LLM.
HIRING_THREAD_MARKERS = ("who is hiring", "who's hiring", "seeking freelancer", "freelancer?")


class HackerNewsScraper(BaseScraper):
    """Stories and comments from the last `max_age_minutes` that look like hiring calls."""

    name = "hackernews"

    async def _scrape(self) -> list[RawLead]:
        # Small safety margin so posts at the window edge are not lost between runs.
        since = int(time.time()) - (self.max_age_minutes + 5) * 60
        payloads = await asyncio.gather(
            *(
                self.fetch_json(config.HN_SEARCH_URL, params=config.build_hn_params(since, query))
                for query in config.HN_QUERIES
            )
        )

        leads: list[RawLead] = []
        seen: set[str] = set()
        for payload in payloads:
            hits = payload.get("hits") if isinstance(payload, dict) else None
            if not isinstance(hits, list):
                continue
            for hit in hits:
                if not isinstance(hit, dict):
                    continue
                lead = self._to_lead(hit)
                if lead is not None and lead.item_id not in seen:
                    seen.add(lead.item_id)
                    leads.append(lead)
        return leads

    def _to_lead(self, hit: dict[str, Any]) -> RawLead | None:
        object_id = hit.get("objectID")
        if not object_id:
            return None

        posted_at = parse_timestamp(hit.get("created_at_i") or hit.get("created_at"))
        if not self.is_fresh(posted_at):
            return None

        tags = hit.get("_tags") or []
        is_comment = "comment" in tags
        thread_title = html_to_text(hit.get("story_title"))
        if any(marker in thread_title.lower() for marker in SELF_PROMO_THREAD_MARKERS):
            return None

        if is_comment:
            if not any(marker in thread_title.lower() for marker in HIRING_THREAD_MARKERS):
                return None
            text = html_to_text(hit.get("comment_text"))
            if not text:
                return None
            first_line = text.splitlines()[0].strip()
            if first_line.lower().lstrip("*#- ").startswith(SELF_PROMO_COMMENT_STARTS):
                return None
            title = first_line[:TITLE_MAX_CHARS].rstrip()
            body = f"{text}\n\n(Thread: {thread_title})" if thread_title else text
        else:
            title = html_to_text(hit.get("title"))
            body = html_to_text(hit.get("story_text"))
            if not title:
                return None

        if not title:
            return None

        return RawLead(
            item_id=make_item_id("hackernews", object_id),
            source="hackernews",
            title=title,
            url=config.HN_ITEM_URL_TEMPLATE.format(object_id=object_id),
            body=body,
            author=str(hit.get("author") or ""),
            posted_at=posted_at,
            hint_category=guess_category(f"{title}\n{body}"),
            extra={
                "type": "comment" if is_comment else "story",
                "thread_title": thread_title or None,
                "external_url": hit.get("url"),
                "points": hit.get("points"),
            },
        )
