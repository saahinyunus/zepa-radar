"""Himalayas remote-jobs API (free, no key): only contract, part-time and temporary roles.

Himalayas lists mostly employment, but it labels every job with an `employmentType`, so the
freelance-like ones can be selected exactly instead of guessing from keywords. Its terms ask for a
visible source link and the unchanged `applicationLink` on every apply button (the lead URL is that
link) and say the data only refreshes about daily, so it is polled every few hours, not every run.
"""
from __future__ import annotations

import logging
from typing import Any

from src import config
from src.models import RawLead, make_item_id, parse_timestamp, utc_now
from src.scrapers.base_scraper import BaseScraper, guess_category, html_to_text

logger = logging.getLogger(__name__)

BODY_MAX_CHARS = 4000


def _epoch(value: Any):
    """Himalayas gives epoch seconds, sometimes as a string."""
    try:
        return parse_timestamp(float(value))
    except (TypeError, ValueError):
        return parse_timestamp(value)


def _list(value: Any) -> list[str]:
    return [str(item) for item in value if item] if isinstance(value, list) else []


class HimalayasScraper(BaseScraper):
    """Newest contract/part-time/temporary remote roles, newest first."""

    name = "himalayas"

    def __init__(self, *args: Any, throttle: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Scheduled runs only: the API refreshes about daily, so poll every few hours.
        self.throttle = throttle

    def _due(self) -> bool:
        if not self.throttle:
            return True
        now = utc_now()
        return now.hour % config.HIMALAYAS_POLL_HOURS == 0 and now.minute < config.HOURLY_BOARD_MINUTE_WINDOW

    async def _scrape(self) -> list[RawLead]:
        if not self._due():
            logger.info("[himalayas] skipped (polled every %d h)", config.HIMALAYAS_POLL_HOURS)
            return []

        leads: list[RawLead] = []
        cursor: str | None = None
        for _ in range(config.HIMALAYAS_MAX_PAGES):
            payload = await self.fetch_json(config.HIMALAYAS_API_URL, params={"cursor": cursor} if cursor else None)
            jobs = payload.get("jobs") if isinstance(payload, dict) else None
            if not isinstance(jobs, list) or not jobs:
                break
            page_has_fresh = False
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                if self.is_fresh(_epoch(job.get("pubDate"))):
                    page_has_fresh = True
                lead = self._to_lead(job)
                if lead is not None:
                    leads.append(lead)
            cursor = payload.get("nextCursor")
            # Newest first: once a whole page is older than the window, later pages are too.
            if not cursor or not page_has_fresh:
                break
        return leads

    def _to_lead(self, job: dict[str, Any]) -> RawLead | None:
        employment = str(job.get("employmentType") or "").strip()
        if employment.lower() not in config.HIMALAYAS_FLEXIBLE_TYPES:
            return None
        title = html_to_text(job.get("title"))
        link = str(job.get("applicationLink") or job.get("guid") or "")
        if not title or not link:
            return None
        posted_at = _epoch(job.get("pubDate"))
        if not self.is_fresh(posted_at):
            return None

        extra: dict[str, Any] = {"employment_type": employment, "company": job.get("companyName")}
        lines = [f"Employment type: {employment}"]
        if job.get("companyName"):
            lines.append(f"Company: {job['companyName']}")

        # Only an hourly USD rate is a usable pay figure; annual salaries are not project budgets.
        low, high = job.get("minSalary"), job.get("maxSalary")
        period = str(job.get("salaryPeriod") or "").lower()
        if str(job.get("currency") or "").upper() == "USD" and "hour" in period and isinstance(high, (int, float)):
            low = low if isinstance(low, (int, float)) else high
            lines.append(f"Pay: ${low:,.0f} - ${high:,.0f} per hour (USD)")
            extra.update(budget_usd_min=low, budget_usd_max=high, type="hourly", budget_kind="explicit")

        places = _list(job.get("locationRestrictions"))
        lines.append("Location restrictions: " + (", ".join(places) if places else "none stated (worldwide)"))
        categories = _list(job.get("categories")) + _list(job.get("parentCategories"))
        description = html_to_text(job.get("description") or job.get("excerpt"))
        body = "\n".join([*lines, "", description]).strip()[:BODY_MAX_CHARS]

        return RawLead(
            item_id=make_item_id("himalayas", job.get("guid") or link),
            source="himalayas",
            title=title,
            url=link,
            body=body,
            author=str(job.get("companyName") or ""),
            posted_at=posted_at,
            hint_category=guess_category(f"{title} {' '.join(categories)}"),
            extra=extra,
        )
