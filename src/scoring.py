"""Deterministic lead scoring.

The LLM only extracts facts from a listing (engagement type, scope clarity, red flags).
The score itself is computed here from those facts plus structured data the scrapers
already know (budget, bid count, age, description length), so the same listing always
gets the same score and every point can be traced back to a reason.

    Component            Points   Source
    -------------------  -------  ------------------------------------------
    base                 10       any lead that passes the validity checks
    budget               0-30     scraper data, else dollar amounts in the text
    scope clarity        0-15     LLM rating 1-5
    description detail   0-10     text length
    competition          0-15     bids so far (Freelancer) / direct contact
    freshness            0-10     minutes since posting
    engagement type      0-10     LLM: project / ongoing / full-time / unclear
    red flags            -12 each LLM list, three of them are disqualifying
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from src.models import RawLead, utc_now

FATAL_FLAGS = frozenset({"pay_to_apply", "scam_or_mlm", "adult_or_illegal"})
SOFT_FLAGS = frozenset({"spec_work", "unrealistic_budget", "vague_template", "commission_only"})
RED_FLAG_PENALTY = 12

ENGAGEMENT_POINTS = {
    "one_off_project": 10,
    "ongoing_contract": 9,
    "unclear": 5,
    "full_time_job": 1,
}
FULL_TIME_SCORE_CAP = 55
# Budget points when the listing names no amount: neutral, not a reward.
UNKNOWN_BUDGET_POINTS = 12
# Amounts guessed from free text are less reliable than structured data.
TEXT_BUDGET_POINTS_CAP = 21
# Platforms where many freelancers bid on the same listing leave little room for a late applicant.
BIDDING_SOURCES = frozenset({"freelancer"})
UNKNOWN_BIDS_POINTS = 10

_AMOUNT = re.compile(r"[$€£]\s?(\d[\d,]*(?:\.\d+)?)\s?(k\b)?", re.IGNORECASE)
_HOURLY = re.compile(r"(?:/|\bper\s)\s?(?:hr|hour)\b|\bhourly\b", re.IGNORECASE)


@dataclass(slots=True)
class ScoreResult:
    score: int
    valid: bool = True
    parts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        breakdown = " ".join(f"{name}={points:+d}" for name, points in self.parts.items())
        return f"{self.score} [{breakdown}]" + (f" {'; '.join(self.notes)}" if self.notes else "")


def parse_amount_usd(text: str) -> tuple[float | None, bool]:
    """Largest currency amount found in free text, and whether it reads as hourly."""
    best: float | None = None
    for number, thousands in _AMOUNT.findall(text or ""):
        try:
            value = float(number.replace(",", ""))
        except ValueError:
            continue
        if thousands:
            value *= 1000
        best = value if best is None else max(best, value)
    return best, bool(_HOURLY.search(text or ""))


def budget_points(amount_usd: float | None, hourly: bool) -> int:
    if amount_usd is None:
        return UNKNOWN_BUDGET_POINTS
    tiers = (
        ((60, 30), (35, 24), (20, 16), (12, 8))
        if hourly
        else ((3000, 30), (1500, 26), (750, 21), (300, 15), (150, 8))
    )
    for threshold, points in tiers:
        if amount_usd >= threshold:
            return points
    return 2 if hourly else 3


def detail_points(length: int) -> int:
    for threshold, points in ((1200, 10), (600, 7), (300, 4), (120, 2)):
        if length >= threshold:
            return points
    return 0


def competition_points(source: str, bids: Any) -> int:
    if source in BIDDING_SOURCES:
        if not isinstance(bids, (int, float)):
            return UNKNOWN_BIDS_POINTS
        for limit, points in ((5, 15), (15, 11), (30, 7), (60, 3)):
            if bids <= limit:
                return points
        return 0
    # Reddit / HN / job boards: the client is contacted directly, no public bidding war.
    return 12


def freshness_points(raw: RawLead) -> int:
    if raw.posted_at is None:
        return 5
    age_minutes = (utc_now() - raw.posted_at).total_seconds() / 60
    for limit, points in ((30, 10), (60, 8), (120, 5), (240, 3)):
        if age_minutes <= limit:
            return points
    return 1


def clarity_points(value: Any) -> int:
    try:
        rating = max(1, min(5, int(round(float(value)))))
    except (TypeError, ValueError):
        rating = 2
    return round((rating - 1) * 3.75)


def normalize_flags(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set)):
        return set()
    known = FATAL_FLAGS | SOFT_FLAGS
    return {flag for flag in (str(item).strip().lower() for item in value) if flag in known}


def normalize_engagement(value: Any) -> str:
    text = re.sub(r"[^a-z]+", "_", str(value or "").lower()).strip("_")
    return text if text in ENGAGEMENT_POINTS else "unclear"


def score_lead(raw: RawLead, facts: Mapping[str, Any]) -> ScoreResult:
    """Score one lead from LLM-extracted `facts` and the scraper's own data."""
    flags = normalize_flags(facts.get("red_flags"))
    if flags & FATAL_FLAGS:
        return ScoreResult(0, valid=False, notes=[f"disqualified: {', '.join(sorted(flags & FATAL_FLAGS))}"])

    extra = raw.extra or {}
    notes: list[str] = []

    structured = extra.get("budget_usd_max")
    if isinstance(structured, (int, float)) and structured > 0:
        hourly = extra.get("type") == "hourly"
        budget = budget_points(float(structured), hourly)
    else:
        amount, hourly = parse_amount_usd(f"{raw.title}\n{raw.body}")
        budget = budget_points(amount, hourly)
        if amount is not None:
            budget = min(budget, TEXT_BUDGET_POINTS_CAP)

    engagement = normalize_engagement(facts.get("engagement"))
    parts = {
        "base": 10,
        "budget": budget,
        "scope": clarity_points(facts.get("scope_clarity")),
        "detail": detail_points(len((raw.body or "").strip())),
        "competition": competition_points(raw.source, extra.get("bids")),
        "fresh": freshness_points(raw),
        "engagement": ENGAGEMENT_POINTS[engagement],
    }
    soft = sorted(flags & SOFT_FLAGS)
    if soft:
        parts["flags"] = -RED_FLAG_PENALTY * len(soft)
        notes.append(f"flags: {', '.join(soft)}")

    score = max(0, min(100, sum(parts.values())))
    if engagement == "full_time_job" and score > FULL_TIME_SCORE_CAP:
        score = FULL_TIME_SCORE_CAP
        notes.append("capped: permanent full-time role")
    return ScoreResult(score, parts=parts, notes=notes)
