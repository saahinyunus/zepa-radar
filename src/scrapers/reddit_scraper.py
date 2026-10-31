"""Reddit scraper: one request per 10-subreddit group (`r/a+b+c/new.json`)."""
from __future__ import annotations

import asyncio
import logging
import random
import xml.etree.ElementTree as ET
from typing import Any

from src import config
from src.models import RawLead, make_item_id, parse_timestamp
from src.scrapers.base_scraper import BaseScraper, guess_category, html_to_text

logger = logging.getLogger(__name__)

ATOM_NS = "{http://www.w3.org/2005/Atom}"
REDDIT_BASE_URL = "https://www.reddit.com"
# Reddit sometimes blocks datacenter IPs on www; old.reddit.com is a second chance.
FALLBACK_HOST = "https://old.reddit.com"
# Reddit answers rapid RSS requests with 429, so groups are spaced out and a failed group gets
# one more try after a longer pause.
PAUSE_BETWEEN_GROUPS_SECONDS = 6.0
RETRY_FAILED_GROUP_AFTER_SECONDS = 20.0

SUBREDDIT_CATEGORY_HINTS = {
    "designjobs": config.CATEGORY_DESIGN,
    "graphic_design": config.CATEGORY_DESIGN,
    "hungryartists": config.CATEGORY_DESIGN,
    "hireaneditor": config.CATEGORY_VIDEO,
    "videoediting": config.CATEGORY_VIDEO,
    "copywriting": config.CATEGORY_MARKETING_WRITING,
    "seo": config.CATEGORY_MARKETING_WRITING,
    "bigseo": config.CATEGORY_MARKETING_WRITING,
    "marketing": config.CATEGORY_MARKETING_WRITING,
    "digitalmarketing": config.CATEGORY_MARKETING_WRITING,
    "ppc": config.CATEGORY_MARKETING_WRITING,
    "content_marketing": config.CATEGORY_MARKETING_WRITING,
    "socialmedia": config.CATEGORY_MARKETING_WRITING,
    "growthhacking": config.CATEGORY_MARKETING_WRITING,
    "webdev": config.CATEGORY_SOFTWARE,
    "shopify": config.CATEGORY_SOFTWARE,
    "wordpress": config.CATEGORY_SOFTWARE,
    "nocode": config.CATEGORY_SOFTWARE,
    "automate": config.CATEGORY_SOFTWARE,
    "saas": config.CATEGORY_SOFTWARE,
    "pythonjobs": config.CATEGORY_SOFTWARE,
    "webdevjobs": config.CATEGORY_SOFTWARE,
    "seojobs": config.CATEGORY_MARKETING_WRITING,
    "hireawriter": config.CATEGORY_MARKETING_WRITING,
    "microsaas": config.CATEGORY_SOFTWARE,
}

_SKIPPED_BODIES = {"[removed]", "[deleted]"}
_SKIPPED_AUTHORS = {"automoderator", "[deleted]"}


class RedditScraper(BaseScraper):
    """Fetches the newest 100 posts of each subreddit group."""

    name = "reddit"

    def __init__(self, *args: Any, rotate: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # With rotate=True only `REDDIT_GROUPS_PER_RUN` groups are fetched per run,
        # chosen at random (keeps Reddit's 429s away).
        self.rotate = rotate
        # Set once the JSON endpoints answer 403 (typical for datacenter IPs);
        # the remaining groups then go straight to the RSS feed.
        self._json_blocked = False

    def _select_groups(self) -> list[tuple[str, str]]:
        groups = list(config.REDDIT_GROUPS.items())
        per_run = config.REDDIT_GROUPS_PER_RUN
        if not self.rotate or per_run <= 0 or per_run >= len(groups):
            return groups
        # The hiring-board group is where real client posts are, so it is fetched on every run;
        # the discussion-heavy groups share the remaining slots at random.
        fixed = [group for group in groups if group[0] in config.REDDIT_ALWAYS_GROUPS]
        rest = [group for group in groups if group[0] not in config.REDDIT_ALWAYS_GROUPS]
        slots = max(0, per_run - len(fixed))
        return fixed + random.sample(rest, min(slots, len(rest)))

    async def _scrape(self) -> list[RawLead]:
        leads: list[RawLead] = []
        seen: set[str] = set()

        selected = self._select_groups()
        logger.info("[reddit] fetching group(s): %s", ", ".join(name for name, _ in selected))
        failed: list[tuple[str, str]] = []
        for index, (group_name, group) in enumerate(selected):
            if index > 0:
                await asyncio.sleep(PAUSE_BETWEEN_GROUPS_SECONDS)
            group_leads = await self._fetch_group(group)
            if group_leads is None:
                failed.append((group_name, group))
                continue
            self._collect(group_leads, leads, seen)

        if failed:
            await asyncio.sleep(RETRY_FAILED_GROUP_AFTER_SECONDS)
            for index, (group_name, group) in enumerate(failed):
                if index > 0:
                    await asyncio.sleep(PAUSE_BETWEEN_GROUPS_SECONDS)
                group_leads = await self._fetch_group(group)
                if group_leads is None:
                    logger.warning("[reddit] group '%s' unavailable, skipping", group_name)
                    continue
                logger.info("[reddit] group '%s' succeeded on the second try", group_name)
                self._collect(group_leads, leads, seen)
        return leads

    @staticmethod
    def _collect(group_leads: list[RawLead], leads: list[RawLead], seen: set[str]) -> None:
        for lead in group_leads:
            if lead.item_id not in seen:
                seen.add(lead.item_id)
                leads.append(lead)

    async def _fetch_group(self, group: str) -> list[RawLead] | None:
        """JSON first (spec), then old.reddit.com JSON, then the RSS/Atom feed."""
        headers = {"User-Agent": config.REDDIT_USER_AGENT}

        if not self._json_blocked:
            json_url = config.REDDIT_URL_TEMPLATE.format(group=group)
            # raw_json=1 stops Reddit from HTML-escaping &, < and > in titles.
            for candidate in (json_url, json_url.replace(REDDIT_BASE_URL, FALLBACK_HOST, 1)):
                payload = await self.fetch_json(f"{candidate}&raw_json=1", headers=headers)
                if payload is not None:
                    return self._parse_listing(payload)
            self._json_blocked = True
            logger.warning("[reddit] JSON endpoints blocked, switching to RSS feeds")

        feed = await self.fetch_text(
            f"{REDDIT_BASE_URL}/r/{group}/new.rss?limit=100",
            headers=headers,
            accept="application/atom+xml, application/rss+xml, application/xml, */*",
        )
        if not feed:
            return None
        return self._parse_atom(feed)

    def _parse_atom(self, feed: str) -> list[RawLead]:
        try:
            root = ET.fromstring(feed.encode("utf-8"))
        except ET.ParseError as exc:
            logger.error("[reddit] invalid Atom feed: %s", exc)
            return []

        def text_of(entry: ET.Element, tag: str) -> str:
            return (entry.findtext(f"{ATOM_NS}{tag}") or "").strip()

        leads: list[RawLead] = []
        for entry in root.iter(f"{ATOM_NS}entry"):
            fullname = text_of(entry, "id")
            title = html_to_text(text_of(entry, "title"))
            link = entry.find(f"{ATOM_NS}link")
            url = (link.get("href") if link is not None else "") or ""
            if not fullname.startswith("t3_") or not title or not url:
                continue

            posted_at = parse_timestamp(text_of(entry, "published") or text_of(entry, "updated"))
            if not self.is_fresh(posted_at):
                continue

            author = entry.findtext(f"{ATOM_NS}author/{ATOM_NS}name") or ""
            author = author.strip().removeprefix("/u/")
            if author.lower() in _SKIPPED_AUTHORS:
                continue

            body = html_to_text(text_of(entry, "content"))
            # Drop the "submitted by /u/x to r/y [link] [comments]" footer Reddit appends.
            footer = body.find("submitted by")
            if footer != -1:
                body = body[:footer].strip()
            if body in _SKIPPED_BODIES:
                continue

            category_node = entry.find(f"{ATOM_NS}category")
            subreddit = (category_node.get("term") if category_node is not None else "") or ""
            hint = SUBREDDIT_CATEGORY_HINTS.get(subreddit.lower()) or guess_category(f"{title}\n{body}")

            leads.append(
                RawLead(
                    item_id=make_item_id("reddit", fullname),
                    source="reddit",
                    title=title,
                    url=url,
                    body=body,
                    author=author,
                    posted_at=posted_at,
                    hint_category=hint,
                    extra={"subreddit": subreddit, "via": "rss"},
                )
            )
        return leads

    def _parse_listing(self, payload: Any) -> list[RawLead]:
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        children = data.get("children") if isinstance(data, dict) else None
        if not isinstance(children, list):
            return []

        leads: list[RawLead] = []
        for child in children:
            post = child.get("data") if isinstance(child, dict) else None
            if not isinstance(post, dict):
                continue
            lead = self._to_lead(post)
            if lead is not None:
                leads.append(lead)
        return leads

    def _to_lead(self, post: dict[str, Any]) -> RawLead | None:
        fullname = post.get("name")
        title = str(post.get("title") or "").strip()
        permalink = post.get("permalink")
        if not fullname or not title or not permalink:
            return None
        if post.get("stickied"):
            return None

        author = str(post.get("author") or "")
        if author.lower() in _SKIPPED_AUTHORS:
            return None

        body = str(post.get("selftext") or "").strip()
        if body in _SKIPPED_BODIES:
            return None

        posted_at = parse_timestamp(post.get("created_utc"))
        if not self.is_fresh(posted_at):
            return None

        subreddit = str(post.get("subreddit") or "")
        hint = SUBREDDIT_CATEGORY_HINTS.get(subreddit.lower()) or guess_category(f"{title}\n{body}")

        return RawLead(
            item_id=make_item_id("reddit", fullname),
            source="reddit",
            title=title,
            url=f"{REDDIT_BASE_URL}{permalink}",
            body=body,
            author=author,
            posted_at=posted_at,
            hint_category=hint,
            extra={
                "subreddit": subreddit,
                "flair": post.get("link_flair_text"),
                "score": post.get("score"),
                "num_comments": post.get("num_comments"),
                "external_url": post.get("url_overridden_by_dest"),
            },
        )
