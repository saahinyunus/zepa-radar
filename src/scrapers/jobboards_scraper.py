"""Remote job boards: RemoteOK, Jobicy, Arbeitnow (JSON) and WeWorkRemotely (RSS)."""
from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Awaitable, Callable

from src import config
from src.models import RawLead, make_item_id, parse_timestamp, utc_now
from src.scrapers.base_scraper import BaseScraper, guess_category, html_to_text

logger = logging.getLogger(__name__)

BODY_MAX_CHARS = 4000

# Permanent employee roles are useless for a freelance radar and burn LLM quota, so a
# job-board post must show a flexible-work signal in its title or tags/job type (the
# description is not scanned: boilerplate there lets permanent roles through).
FLEXIBLE_WORK = re.compile(
    r"\b(freelanc\w*|contract(?:or|ors|s)?|part[- ]?time|gig|temporary|temp|project[- ]based|one[- ]off)\b",
    re.IGNORECASE,
)
# Regular employment contracts (Arbeitnow tags such as "Indefinite contract") are not freelance work.
EMPLOYMENT_CONTRACT = re.compile(
    r"\b(?:indefinite|fixed[- ]term|permanent|unbefristet\w*|befristet\w*|tijdelijk|vast)\s+(?:employment\s+)?(?:contract|arbeits\w*)[^,;]*",
    re.IGNORECASE,
)


def _salary_text(low: Any, high: Any, currency: str = "$") -> str:
    """Human readable salary range, or '' when the board gives none."""
    def fmt(value: Any) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return ""
        return f"{currency}{number:,.0f}" if number > 0 else ""

    low_text, high_text = fmt(low), fmt(high)
    if low_text and high_text:
        return f"{low_text} - {high_text}"
    return low_text or high_text


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


class JobBoardsScraper(BaseScraper):
    """Fetches all job boards concurrently; each board failure is isolated."""

    name = "jobboards"

    def __init__(self, *args: Any, throttle: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Scheduled runs only: respect boards that limit how often they may be polled.
        self.throttle = throttle

    def _jobicy_due(self) -> bool:
        return not self.throttle or utc_now().minute < config.HOURLY_BOARD_MINUTE_WINDOW

    async def _scrape(self) -> list[RawLead]:
        enabled = config.ENABLED_JOB_BOARDS
        tasks: list[tuple[str, Awaitable[list[RawLead]]]] = []
        if "remoteok" in enabled:
            tasks.append(("remoteok", self._remoteok()))
        if "arbeitnow" in enabled:
            tasks.append(("arbeitnow", self._arbeitnow()))
        if "jobicy" in enabled:
            if self._jobicy_due():
                tasks.append(("jobicy", self._jobicy()))
            else:
                logger.info("[jobboards] Jobicy skipped (polled at most once per hour)")
        if "weworkremotely" in enabled:
            for category, feed_url in config.WWR_RSS_FEEDS.items():
                tasks.append((f"weworkremotely/{category}", self._weworkremotely(category, feed_url)))

        results = await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)

        leads: list[RawLead] = []
        seen: set[str] = set()
        for (label, _), result in zip(tasks, results):
            if isinstance(result, BaseException):
                logger.error("[jobboards] %s failed: %r", label, result)
                continue
            for lead in result:
                if lead.item_id not in seen:
                    seen.add(lead.item_id)
                    leads.append(lead)
        return leads

    # --- shared builder ----------------------------------------------------
    def _build_lead(
        self,
        *,
        source: str,
        native_id: Any,
        title: str,
        url: str,
        description: str,
        company: str,
        posted_at: datetime | None,
        tags: list[str],
        extra: dict[str, Any],
        fallback_category: str | None = None,
    ) -> RawLead | None:
        title = title.strip()
        if not native_id or not title or not url:
            return None
        if not self.is_fresh(posted_at):
            return None

        # Title only: board tags are broad ("Marketing", "Design" on sales roles) and
        # would let unrelated jobs through to the LLM.
        category = guess_category(title) or fallback_category
        if category is None:
            # Not a SOFTWARE/DESIGN/VIDEO/MARKETING role - do not waste LLM quota.
            return None
        scan_text = EMPLOYMENT_CONTRACT.sub(" ", " ".join([title, *tags]))
        if not FLEXIBLE_WORK.search(scan_text):
            # Permanent employee role - not freelance work, do not waste LLM quota.
            return None

        header = f"Company: {company}\n" if company else ""
        tag_line = f"Tags: {', '.join(tags)}\n" if tags else ""
        body = f"{header}{tag_line}\n{description}".strip()[:BODY_MAX_CHARS]

        return RawLead(
            item_id=make_item_id(source, native_id),
            source=source,
            title=title,
            url=url,
            body=body,
            author=company,
            posted_at=posted_at,
            hint_category=category,
            extra=extra,
        )

    async def _json_board(
        self, url: str, parser: Callable[[Any], list[RawLead]]
    ) -> list[RawLead]:
        payload = await self.fetch_json(url)
        if payload is None:
            return []
        return parser(payload)

    # --- RemoteOK ----------------------------------------------------------
    async def _remoteok(self) -> list[RawLead]:
        return await self._json_board(config.REMOTEOK_API_URL, self._parse_remoteok)

    def _parse_remoteok(self, payload: Any) -> list[RawLead]:
        if not isinstance(payload, list):
            return []
        leads: list[RawLead] = []
        for job in payload:
            # First element is a legal notice without an id/position.
            if not isinstance(job, dict) or not job.get("id") or not job.get("position"):
                continue
            tags = _as_list(job.get("tags"))
            salary = _salary_text(job.get("salary_min"), job.get("salary_max"))
            description = html_to_text(job.get("description"))
            if salary:
                description = f"Salary: {salary}\n\n{description}"
            lead = self._build_lead(
                source="remoteok",
                native_id=job.get("id"),
                title=html_to_text(job.get("position")),
                url=str(job.get("url") or job.get("apply_url") or ""),
                description=description,
                company=html_to_text(job.get("company")),
                posted_at=parse_timestamp(job.get("epoch") or job.get("date")),
                tags=tags,
                extra={"location": job.get("location"), "salary": salary or None},
            )
            if lead is not None:
                leads.append(lead)
        return leads

    # --- Jobicy ------------------------------------------------------------
    async def _jobicy(self) -> list[RawLead]:
        return await self._json_board(config.JOBICY_API_URL, self._parse_jobicy)

    def _parse_jobicy(self, payload: Any) -> list[RawLead]:
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            return []
        leads: list[RawLead] = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            tags = _as_list(job.get("jobIndustry")) + _as_list(job.get("jobType"))
            currency = {"USD": "$", "EUR": "€", "GBP": "£"}.get(
                str(job.get("salaryCurrency") or "USD").upper(), ""
            ) or str(job.get("salaryCurrency") or "$")
            salary = _salary_text(job.get("annualSalaryMin"), job.get("annualSalaryMax"), currency)
            description = html_to_text(job.get("jobDescription") or job.get("jobExcerpt"))
            if salary:
                description = f"Salary (yearly): {salary}\n\n{description}"
            lead = self._build_lead(
                source="jobicy",
                native_id=job.get("id"),
                title=html_to_text(job.get("jobTitle")),
                url=str(job.get("url") or ""),
                description=description,
                company=html_to_text(job.get("companyName")),
                posted_at=parse_timestamp(job.get("pubDate")),
                tags=tags,
                extra={
                    "location": job.get("jobGeo"),
                    "level": job.get("jobLevel"),
                    "salary": salary or None,
                },
            )
            if lead is not None:
                leads.append(lead)
        return leads

    # --- Arbeitnow ---------------------------------------------------------
    async def _arbeitnow(self) -> list[RawLead]:
        return await self._json_board(config.ARBEITNOW_API_URL, self._parse_arbeitnow)

    def _parse_arbeitnow(self, payload: Any) -> list[RawLead]:
        jobs = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            return []
        leads: list[RawLead] = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            tags = _as_list(job.get("tags")) + _as_list(job.get("job_types"))
            lead = self._build_lead(
                source="arbeitnow",
                native_id=job.get("slug"),
                title=html_to_text(job.get("title")),
                url=str(job.get("url") or ""),
                description=html_to_text(job.get("description")),
                company=html_to_text(job.get("company_name")),
                posted_at=parse_timestamp(job.get("created_at")),
                tags=tags,
                extra={"location": job.get("location"), "remote": job.get("remote")},
            )
            if lead is not None:
                leads.append(lead)
        return leads

    # --- WeWorkRemotely (RSS) ----------------------------------------------
    async def _weworkremotely(self, feed_category: str, feed_url: str) -> list[RawLead]:
        text = await self.fetch_text(feed_url)
        if not text:
            return []
        try:
            root = ET.fromstring(text.encode("utf-8"))
        except ET.ParseError as exc:
            logger.error("[jobboards] invalid WeWorkRemotely RSS (%s): %s", feed_category, exc)
            return []

        leads: list[RawLead] = []
        for item in root.iter("item"):
            raw_title = html_to_text(item.findtext("title"))
            company, _, position = raw_title.partition(":")
            if not position:
                company, position = "", raw_title
            link = (item.findtext("link") or "").strip()
            native_id = (item.findtext("guid") or link).strip()
            tags = [
                value
                for value in (
                    html_to_text(item.findtext("category")),
                    html_to_text(item.findtext("type")),
                )
                if value
            ]
            lead = self._build_lead(
                source="weworkremotely",
                native_id=native_id,
                title=position.strip(),
                url=link,
                description=html_to_text(item.findtext("description")),
                company=company.strip(),
                posted_at=parse_timestamp(item.findtext("pubDate")),
                tags=tags,
                extra={"region": html_to_text(item.findtext("region")) or None},
                fallback_category=feed_category,
            )
            if lead is not None:
                leads.append(lead)
        return leads
