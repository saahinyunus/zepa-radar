"""Freelancer.com scraper using the site's public projects API (no key, no proxy).

Every project is posted by a client who wants to pay for work, so the results are
real leads with budgets. Low-budget and off-category projects are dropped here so
they never cost LLM quota.
"""
from __future__ import annotations

import logging
from typing import Any

from src import config
from src.models import RawLead, make_item_id, parse_timestamp
from src.scrapers.base_scraper import BaseScraper, guess_category, html_to_text

logger = logging.getLogger(__name__)

PROJECT_URL_TEMPLATE = "https://www.freelancer.com/projects/{seo_url}"
BODY_MAX_CHARS = 4000


def _usd(amount: Any, rate: Any) -> float:
    try:
        return float(amount or 0) * float(rate or 1)
    except (TypeError, ValueError):
        return 0.0


def _money(amount: float) -> str:
    return f"{amount:,.0f}"


class FreelancerScraper(BaseScraper):
    """Newest English-language projects, filtered by budget and category."""

    name = "freelancer"

    async def _scrape(self) -> list[RawLead]:
        payload = await self.fetch_json(
            config.FREELANCER_API_URL,
            params=[
                ("limit", config.FREELANCER_FETCH_LIMIT),
                ("job_details", "true"),
                ("full_description", "true"),
                ("sort_field", "time_submitted"),
                ("languages[]", "en"),
            ],
        )
        result = payload.get("result") if isinstance(payload, dict) else None
        projects = result.get("projects") if isinstance(result, dict) else None
        if not isinstance(projects, list):
            return []

        leads: list[RawLead] = []
        for project in projects:
            if not isinstance(project, dict):
                continue
            lead = self._to_lead(project)
            if lead is not None:
                leads.append(lead)
        # Best budgets first; leads cut here are not stored, so they get another chance on
        # the next run while they are still inside the freshness window.
        leads.sort(key=lambda lead: lead.extra.get("budget_usd_max", 0), reverse=True)
        return leads[: config.FREELANCER_MAX_PER_RUN]

    def _to_lead(self, project: dict[str, Any]) -> RawLead | None:
        project_id = project.get("id")
        title = html_to_text(project.get("title"))
        seo_url = project.get("seo_url")
        if not project_id or not title or not seo_url or project.get("status") != "active":
            return None

        posted_at = parse_timestamp(project.get("time_submitted"))
        if not self.is_fresh(posted_at):
            return None

        currency = project.get("currency") or {}
        rate = currency.get("exchange_rate", 1)
        budget = project.get("budget") or {}
        low_usd = _usd(budget.get("minimum"), rate)
        high_usd = _usd(budget.get("maximum"), rate)
        hourly = project.get("type") == "hourly"

        # Fixed projects need a worthwhile ceiling; hourly ones a decent rate.
        min_budget = config.FREELANCER_MIN_HOURLY_USD if hourly else config.FREELANCER_MIN_BUDGET_USD
        if high_usd < min_budget:
            return None

        job_names = [
            str(job.get("name")) for job in project.get("jobs") or [] if isinstance(job, dict) and job.get("name")
        ]
        category = guess_category(f"{title} {' '.join(job_names)}")
        if category is None:
            return None  # data entry, translation, etc. - not one of our categories

        code = str(currency.get("code") or "USD")
        unit = "/hr" if hourly else " (fixed price)"
        budget_line = f"Budget: {_money(low_usd)} - {_money(high_usd)} USD{unit}"
        if code != "USD":
            budget_line += (
                f" (posted as {_money(float(budget.get('minimum') or 0))} - "
                f"{_money(float(budget.get('maximum') or 0))} {code})"
            )

        bid_stats = project.get("bid_stats") or {}
        bids = bid_stats.get("bid_count")
        description = html_to_text(project.get("description") or project.get("preview_description"))
        body = "\n".join(
            part
            for part in (
                budget_line,
                f"Bids so far: {bids}" if bids is not None else "",
                f"Skills: {', '.join(job_names)}" if job_names else "",
                "",
                description,
            )
            if part is not None
        ).strip()[:BODY_MAX_CHARS]

        return RawLead(
            item_id=make_item_id("freelancer", project_id),
            source="freelancer",
            title=title,
            url=PROJECT_URL_TEMPLATE.format(seo_url=seo_url),
            body=body,
            author=str(project.get("owner_id") or ""),
            posted_at=posted_at,
            hint_category=category,
            extra={
                "budget_usd_min": round(low_usd),
                "budget_usd_max": round(high_usd),
                "type": project.get("type"),
                "bids": bids,
            },
        )
