"""Shared async HTTP client, retry/backoff logic and text helpers for all scrapers."""
from __future__ import annotations

import asyncio
import html
import logging
import random
import re
from abc import ABC, abstractmethod
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Mapping

import httpx

from src import config
from src.models import RawLead, utc_now

logger = logging.getLogger(__name__)

_TAG_BREAKS = re.compile(r"(?i)<\s*(?:br|/p|/div|/li|/h[1-6]|/tr)\s*/?\s*>")
_LIST_ITEM = re.compile(r"(?i)<\s*li[^>]*>")
_TAGS = re.compile(r"<[^>]+>")
_BLANK_LINES = re.compile(r"\n\s*\n+")
_SPACES = re.compile(r"[ \t\xa0]+")


def html_to_text(value: Any) -> str:
    """Convert an HTML fragment (job description, HN comment) into plain text."""
    if value is None:
        return ""
    text = str(value)
    text = _TAG_BREAKS.sub("\n", text)
    text = _LIST_ITEM.sub("\n- ", text)
    text = _TAGS.sub(" ", text)
    text = html.unescape(text)
    lines = [_SPACES.sub(" ", line).strip() for line in text.splitlines()]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


_CATEGORY_PATTERNS = {
    category: re.compile(
        r"(?<![a-z0-9])(?:" + "|".join(re.escape(word) for word in words) + r")(?![a-z0-9])"
    )
    for category, words in config.CATEGORY_KEYWORDS.items()
}


def guess_category(text: str) -> str | None:
    """Best-effort category from keyword hits (None when nothing matches).

    This is only a hint; the LLM makes the final classification.
    """
    lowered = (text or "").lower()
    best: str | None = None
    best_hits = 0
    for category, pattern in _CATEGORY_PATTERNS.items():
        hits = len(pattern.findall(lowered))
        if hits > best_hits:
            best, best_hits = category, hits
    return best


def parse_retry_after(headers: Mapping[str, str]) -> float | None:
    """Seconds from a Retry-After header, capped at the max backoff."""
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, min(float(value), config.HTTP_BACKOFF_MAX_SECONDS))
    except ValueError:
        return None


def build_http_client() -> httpx.AsyncClient:
    """Shared client: redirects on, bounded pool, sane timeouts."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(config.HTTP_TIMEOUT_SECONDS, connect=10.0),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        headers={"Accept-Language": "en-US,en;q=0.9"},
    )


class BaseScraper(ABC):
    """Base class: HTTP with retries, User-Agent rotation and freshness checks.

    Subclasses implement `_scrape()`. Callers use `scrape()`, which never raises:
    a failing source is logged and yields an empty list so other sources continue.
    """

    name = "base"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        max_age_minutes: int | None = None,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        # Response statuses per host (e.g. {"www.reddit.com:403": 2}); used for run reports.
        self.http_stats: Counter[str] = Counter()
        self.max_age_minutes = (
            config.LEAD_MAX_AGE_MINUTES if max_age_minutes is None else max_age_minutes
        )

    async def __aenter__(self) -> "BaseScraper":
        self._ensure_client()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = build_http_client()
            self._owns_client = True
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    # --- HTTP --------------------------------------------------------------
    async def fetch(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        accept: str = "application/json, text/plain, */*",
    ) -> httpx.Response | None:
        """GET with exponential backoff. Returns None on permanent failure."""
        client = self._ensure_client()
        last_error = "unknown error"

        for attempt in range(config.HTTP_MAX_RETRIES):
            request_headers = {"User-Agent": random.choice(config.USER_AGENTS), "Accept": accept}
            if headers:
                request_headers.update(headers)
            retry_after: float | None = None

            host = httpx.URL(url).host
            try:
                response = await client.get(url, params=params, headers=request_headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                self.http_stats[f"{host}:error"] += 1
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("[%s] request error (attempt %d): %s", self.name, attempt + 1, last_error)
            else:
                status = response.status_code
                self.http_stats[f"{host}:{status}"] += 1
                if status in config.RETRYABLE_STATUS_CODES:
                    last_error = f"HTTP {status}"
                    retry_after = parse_retry_after(response.headers)
                    logger.warning(
                        "[%s] HTTP %d from %s (attempt %d)", self.name, status, url, attempt + 1
                    )
                elif status >= 400:
                    logger.warning("[%s] HTTP %d from %s - giving up", self.name, status, url)
                    return None
                else:
                    return response

            if attempt < config.HTTP_MAX_RETRIES - 1:
                await asyncio.sleep(retry_after if retry_after is not None else config.backoff_delay(attempt))

        logger.error(
            "[%s] %s failed after %d attempts: %s", self.name, url, config.HTTP_MAX_RETRIES, last_error
        )
        return None

    async def fetch_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any | None:
        response = await self.fetch(url, params=params, headers=headers)
        if response is None:
            return None
        try:
            return response.json()
        except ValueError:
            logger.error("[%s] invalid JSON from %s", self.name, url)
            return None

    async def fetch_text(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        accept: str = "application/rss+xml, application/xml, text/xml, */*",
    ) -> str | None:
        response = await self.fetch(url, params=params, headers=headers, accept=accept)
        return response.text if response is not None else None

    # --- freshness ---------------------------------------------------------
    @property
    def oldest_allowed(self) -> datetime:
        return utc_now() - timedelta(minutes=self.max_age_minutes)

    def is_fresh(self, posted_at: datetime | None) -> bool:
        """Unknown dates count as fresh; database deduplication handles repeats."""
        return posted_at is None or posted_at >= self.oldest_allowed

    # --- public entry point ------------------------------------------------
    async def scrape(self) -> list[RawLead]:
        try:
            leads = await self._scrape()
        except Exception:  # noqa: BLE001 - one broken source must not stop the run
            logger.exception("[%s] scraper crashed", self.name)
            return []
        logger.info("[%s] collected %d fresh lead(s)", self.name, len(leads))
        return leads

    @abstractmethod
    async def _scrape(self) -> list[RawLead]:
        """Fetch and parse the source into RawLead objects."""
        raise NotImplementedError
