"""Deterministic lead scoring (version 2).

The LLM only extracts facts from a listing (engagement type, scope clarity, remote/digital,
geographic restriction, red flags). The score itself is computed here from those facts plus
structured data the scrapers already know (budget, bids, age), so the same listing always gets
the same score and every point can be traced back to a reason.

    Component          Points    Source
    -----------------  --------  -------------------------------------------------
    base               10        any lead that passes the validity checks
    budget             2-25      explicit amount, else a platform range, else neutral
    scope clarity      0-20      LLM rating 1-5
    competition        0-12      bids so far (bidding platforms) / direct contact
    freshness          1-10      minutes since the original posting
    engagement type    1-10      LLM: project / ongoing / full-time / unclear
    geography          0 to -25  LLM: worldwide / region / one country
    red flags          -12 each  LLM list; three of them are disqualifying

Weights are provisional. Every lead stores its facts and the score breakdown (`facts`), so the
weights and the publication threshold can be re-checked against labelled leads instead of guessed.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from src.models import RawLead, utc_now

SCORE_VERSION = 2

FATAL_FLAGS = frozenset({"pay_to_apply", "scam_or_mlm", "adult_or_illegal", "policy_violating"})
SOFT_FLAGS = frozenset({"spec_work", "unrealistic_budget", "vague_template", "commission_only"})
RED_FLAG_PENALTY = 12

ENGAGEMENT_POINTS = {
    "one_off_project": 10,
    "ongoing_contract": 9,
    "unclear": 5,
    "full_time_job": 1,
}
FULL_TIME_SCORE_CAP = 55

GEO_RESTRICTIONS = ("worldwide", "us_only", "eu_only", "specific_country", "unknown")
# Members work from everywhere: a job tied to one country is out of reach for most of them.
GEO_PENALTY = {"worldwide": 0, "unknown": 0, "us_only": 8, "eu_only": 8, "specific_country": 25}

# Budget provenance: how much the amount can be trusted.
BUDGET_EXPLICIT = "explicit"  # the client wrote it (structured field or amount in the text)
BUDGET_PLATFORM_RANGE = "platform_range"  # a preset size class picked from a menu (Freelancer.com)
BUDGET_UNKNOWN = "unknown"
UNKNOWN_BUDGET_POINTS = 10
# A preset size class is not an exact client figure: platform presets (a poster posted as a
# $50,000-100,000 "very large project") say little about the real budget, so they cap out low.
NON_EXACT_BUDGET_POINTS_CAP = 15

# Direct contact (Reddit / HN / job boards) has no public bidding war. A manual audit found these
# leads the most useful ones, so they earn more than the typical bidding-platform lead
# (median 20+ bids) and about as much as its best case.
BIDDING_SOURCES = frozenset({"freelancer"})
DIRECT_CONTACT_POINTS = 12
UNKNOWN_BIDS_POINTS = 4

_AMOUNT = re.compile(r"[$€£]\s?(\d[\d,]*(?:\.\d+)?)\s?(k\b)?", re.IGNORECASE)
_HOURLY = re.compile(r"(?:/|\bper\s)\s?(?:hr|hour)\b|\bhourly\b", re.IGNORECASE)


@dataclass(slots=True)
class ScoreResult:
    score: int
    valid: bool = True
    parts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

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
        ((60, 25), (35, 20), (20, 14), (12, 7))
        if hourly
        else ((3000, 25), (1500, 21), (750, 17), (300, 12), (150, 6))
    )
    for threshold, points in tiers:
        if amount_usd >= threshold:
            return points
    return 2


def representative_amount(low: float, high: float) -> float:
    """Typical value of a budget range: the geometric mean, so a 250-750 preset counts as ~430."""
    if low > 0 and high >= low:
        return math.sqrt(low * high)
    return max(low, high)


def competition_points(source: str, bids: Any) -> int:
    if source in BIDDING_SOURCES:
        if not isinstance(bids, (int, float)):
            return UNKNOWN_BIDS_POINTS
        for limit, points in ((5, 12), (15, 8), (30, 5), (60, 2)):
            if bids <= limit:
                return points
        return 0
    return DIRECT_CONTACT_POINTS


def freshness_points(raw: RawLead) -> int:
    if raw.posted_at is None:
        return 5
    age_minutes = (utc_now() - raw.posted_at).total_seconds() / 60
    for limit, points in ((30, 10), (60, 8), (120, 5), (240, 3)):
        if age_minutes <= limit:
            return points
    return 1


def clarity_rating(value: Any) -> int:
    try:
        return max(1, min(5, int(round(float(value)))))
    except (TypeError, ValueError):
        return 2


def normalize_flags(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set)):
        return set()
    known = FATAL_FLAGS | SOFT_FLAGS
    return {flag for flag in (str(item).strip().lower() for item in value) if flag in known}


def normalize_engagement(value: Any) -> str:
    text = re.sub(r"[^a-z]+", "_", str(value or "").lower()).strip("_")
    return text if text in ENGAGEMENT_POINTS else "unclear"


def normalize_geo(value: Any) -> str:
    text = re.sub(r"[^a-z]+", "_", str(value or "").lower()).strip("_")
    return text if text in GEO_RESTRICTIONS else "unknown"


def normalize_remote_digital(value: Any) -> bool | None:
    """True/False when the LLM committed to an answer, None when it did not say."""
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "yes", "1"}:
        return True
    if text in {"false", "no", "0"}:
        return False
    return None


def _budget(raw: RawLead) -> tuple[str, float | None, bool]:
    """(provenance, amount in USD, hourly) from scraper data or the post text."""
    extra = raw.extra or {}
    high = extra.get("budget_usd_max")
    if isinstance(high, (int, float)) and high > 0:
        low = extra.get("budget_usd_min")
        low = float(low) if isinstance(low, (int, float)) else 0.0
        hourly = extra.get("type") == "hourly"
        if extra.get("budget_kind", BUDGET_PLATFORM_RANGE) == BUDGET_EXPLICIT:
            return BUDGET_EXPLICIT, float(high), hourly
        return BUDGET_PLATFORM_RANGE, representative_amount(low, float(high)), hourly
    amount, hourly = parse_amount_usd(f"{raw.title}\n{raw.body}")
    if amount is not None:
        return BUDGET_EXPLICIT, amount, hourly
    return BUDGET_UNKNOWN, None, False


def score_lead(raw: RawLead, facts: Mapping[str, Any]) -> ScoreResult:
    """Score one lead from LLM-extracted `facts` and the scraper's own data."""
    flags = normalize_flags(facts.get("red_flags"))
    if flags & FATAL_FLAGS:
        return ScoreResult(0, valid=False, notes=[f"disqualified: {', '.join(sorted(flags & FATAL_FLAGS))}"])
    remote_digital = normalize_remote_digital(facts.get("remote_digital"))
    if remote_digital is False:
        return ScoreResult(0, valid=False, notes=["not remote digital work"])

    notes: list[str] = []
    engagement = normalize_engagement(facts.get("engagement"))
    geo = normalize_geo(facts.get("geo_restriction"))
    rating = clarity_rating(facts.get("scope_clarity"))
    kind, amount, hourly = _budget(raw)

    budget = budget_points(amount, hourly)
    if kind != BUDGET_EXPLICIT and amount is not None:
        budget = min(budget, NON_EXACT_BUDGET_POINTS_CAP)

    parts = {
        "base": 10,
        "budget": budget,
        "scope": (rating - 1) * 5,
        "competition": competition_points(raw.source, (raw.extra or {}).get("bids")),
        "fresh": freshness_points(raw),
        "engagement": ENGAGEMENT_POINTS[engagement],
    }
    if GEO_PENALTY[geo]:
        parts["geo"] = -GEO_PENALTY[geo]
        notes.append(f"geo: {geo}")
    soft = sorted(flags & SOFT_FLAGS)
    if soft:
        parts["flags"] = -RED_FLAG_PENALTY * len(soft)
        notes.append(f"flags: {', '.join(soft)}")

    score = max(0, min(100, sum(parts.values())))
    if engagement == "full_time_job" and score > FULL_TIME_SCORE_CAP:
        score = FULL_TIME_SCORE_CAP
        notes.append("capped: permanent full-time role")

    return ScoreResult(
        score,
        parts=parts,
        notes=notes,
        facts={
            "v": SCORE_VERSION,
            "engagement": engagement,
            "scope_clarity": rating,
            "remote_digital": remote_digital,
            "geo": geo,
            "red_flags": sorted(flags),
            "budget_kind": kind,
            "budget_usd": round(amount) if amount is not None else None,
            "bids": (raw.extra or {}).get("bids"),
            "parts": parts,
        },
    )
