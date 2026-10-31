"""Data models shared by scrapers, the AI analyzer, the notifier and the database."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping

from src import config

SUBSCRIBER_STATUS_ACTIVE = "active"
SUBSCRIBER_STATUS_CANCELLED = "cancelled"
SUBSCRIBER_STATUS_EXPIRED = "expired"
SUBSCRIBER_STATUSES = (
    SUBSCRIBER_STATUS_ACTIVE,
    SUBSCRIBER_STATUS_CANCELLED,
    SUBSCRIBER_STATUS_EXPIRED,
)

_CATEGORY_ALIASES = {
    "MARKETING": config.CATEGORY_MARKETING_WRITING,
    "WRITING": config.CATEGORY_MARKETING_WRITING,
    "COPYWRITING": config.CATEGORY_MARKETING_WRITING,
    "SEO": config.CATEGORY_MARKETING_WRITING,
    "MARKETING_AND_WRITING": config.CATEGORY_MARKETING_WRITING,
    "DEV": config.CATEGORY_SOFTWARE,
    "SOFTWARE_DEV": config.CATEGORY_SOFTWARE,
    "GRAPHIC_DESIGN": config.CATEGORY_DESIGN,
    "VIDEO_EDITING": config.CATEGORY_VIDEO,
}


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_db_timestamp(value: datetime) -> str:
    """UTC `YYYY-MM-DD HH:MM:SS` string (matches SQLite CURRENT_TIMESTAMP)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_timestamp(value: Any) -> datetime | None:
    """Parse ISO-8601, RFC-2822 (RSS), epoch seconds or datetime into aware UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, bool):
        return None
    elif isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value).strip()
        if not text:
            return None
        if text[-1] in ("Z", "z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed = parsedate_to_datetime(text)
            except (TypeError, ValueError, IndexError):
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------
def _clip(value: Any, limit: int) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"true", "1", "yes", "y"}


def _coerce_score(value: Any) -> int:
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, score))


def _coerce_int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def load_facts(stored: str) -> dict[str, Any]:
    """Facts JSON kept in `pitch_template_en` (older rows hold a plain-text pitch instead)."""
    if not stored.startswith("{"):
        return {}
    try:
        data = json.loads(stored)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def normalize_category(value: Any, default: str = config.CATEGORY_OTHER) -> str:
    """Map free-form LLM/user category text onto a known category constant."""
    text = re.sub(r"[^A-Z]+", "_", str(value or "").upper()).strip("_")
    if text in config.ALL_CATEGORIES:
        return text
    return _CATEGORY_ALIASES.get(text, default)


def make_item_id(source: str, native_id: Any) -> str:
    """Stable dedupe key, e.g. `reddit:t3_abc123` or `remoteok:123456`."""
    return f"{source.strip().lower()}:{str(native_id).strip()}"


# ---------------------------------------------------------------------------
# Lead models
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class RawLead:
    """A scraped post/job before AI analysis."""

    item_id: str
    source: str
    title: str
    url: str
    body: str = ""
    author: str = ""
    posted_at: datetime | None = None
    hint_category: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def text_for_analysis(self, max_chars: int = 3000) -> str:
        """Compact text block sent to the LLM (title + body, truncated)."""
        body = (self.body or "").strip()
        text = f"Title: {self.title.strip()}\n\n{body}" if body else f"Title: {self.title.strip()}"
        return _clip(text, max_chars)

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "source": self.source,
            "title": self.title,
            "url": self.url,
            "body": self.body,
            "author": self.author,
            "posted_at": self.posted_at.isoformat() if self.posted_at else None,
            "hint_category": self.hint_category,
            "extra": dict(self.extra),
        }


@dataclass(slots=True)
class AnalyzedLead:
    """A lead after Groq analysis; maps 1:1 to a `processed_leads` row."""

    item_id: str
    source: str
    category: str
    title: str
    url: str
    quality_score: int
    estimated_budget: str = "Not specified"
    summary_en: str = ""
    winning_pitch_en: str = ""
    client_pain_point: str = ""
    # Facts the LLM extracted plus the score breakdown (see src/scoring.py). Stored as JSON in the
    # legacy `pitch_template_en` column, which the Telegram product no longer uses for pitches.
    facts: dict[str, Any] = field(default_factory=dict)
    is_actual_client_hiring: bool = True
    sent_to_vip: int = 0
    sent_to_free: int = 0
    posted_at: datetime | None = None
    created_at: datetime | None = None

    @classmethod
    def from_analysis(cls, raw: RawLead, data: Mapping[str, Any]) -> "AnalyzedLead":
        """Build a validated lead from the (untrusted) JSON returned by the LLM."""
        return cls(
            item_id=raw.item_id,
            source=raw.source,
            category=normalize_category(data.get("category")),
            title=_clip(raw.title, 300),
            url=raw.url,
            quality_score=_coerce_score(data.get("quality_score")),
            estimated_budget=_clip(data.get("estimated_budget") or "Not specified", 120),
            summary_en=_clip(data.get("summary_en"), 700),
            winning_pitch_en=_clip(data.get("winning_pitch_en"), 1200),
            client_pain_point=_clip(data.get("client_pain_point"), 300),
            is_actual_client_hiring=_coerce_bool(data.get("is_actual_client_hiring")),
            posted_at=raw.posted_at,
        )

    @classmethod
    def rejected(cls, raw: RawLead, category: str = config.CATEGORY_OTHER) -> "AnalyzedLead":
        """Score-0 placeholder so a rejected post is deduplicated and never re-analysed."""
        return cls(
            item_id=raw.item_id,
            source=raw.source,
            category=normalize_category(category),
            title=_clip(raw.title, 300),
            url=raw.url,
            quality_score=0,
            is_actual_client_hiring=False,
            posted_at=raw.posted_at,
        )

    @property
    def is_valid_lead(self) -> bool:
        """True when the LLM confirmed a real client in a supported category."""
        return self.is_actual_client_hiring and self.category in config.CATEGORIES

    def is_vip_worthy(self, min_score: int | None = None) -> bool:
        threshold = config.VIP_MIN_SCORE if min_score is None else min_score
        return self.is_valid_lead and self.quality_score >= threshold

    def to_db_row(self) -> dict[str, Any]:
        """Column values for `processed_leads` (created_at is set by the database)."""
        return {
            "item_id": self.item_id,
            "source": self.source,
            "category": self.category,
            "title": self.title,
            "url": self.url,
            "quality_score": int(self.quality_score),
            "estimated_budget": self.estimated_budget,
            "summary_en": self.summary_en,
            "pitch_template_en": self.winning_pitch_en or (json.dumps(self.facts, separators=(",", ":")) if self.facts else ""),
            "client_pain_point": self.client_pain_point,
            "sent_to_vip": int(self.sent_to_vip),
            "sent_to_free": int(self.sent_to_free),
        }

    @classmethod
    def from_db_row(cls, row: Mapping[str, Any]) -> "AnalyzedLead":
        score = _coerce_score(row.get("quality_score"))
        stored = str(row.get("pitch_template_en") or "")
        facts = load_facts(stored)
        return cls(
            item_id=str(row["item_id"]),
            source=str(row.get("source") or ""),
            category=normalize_category(row.get("category")),
            title=str(row.get("title") or ""),
            url=str(row.get("url") or ""),
            quality_score=score,
            estimated_budget=str(row.get("estimated_budget") or "Not specified"),
            summary_en=str(row.get("summary_en") or ""),
            winning_pitch_en="" if facts else stored,
            facts=facts,
            client_pain_point=str(row.get("client_pain_point") or ""),
            is_actual_client_hiring=score > 0,
            sent_to_vip=int(row.get("sent_to_vip") or 0),
            sent_to_free=int(row.get("sent_to_free") or 0),
            created_at=parse_timestamp(row.get("created_at")),
        )

    def to_apify_dict(self) -> dict[str, Any]:
        """Flat JSON record pushed to the Apify dataset via `Actor.push_data`."""
        return {
            "id": self.item_id,
            "source": self.source,
            "category": self.category,
            "title": self.title,
            "url": self.url,
            "qualityScore": self.quality_score,
            "estimatedBudget": self.estimated_budget,
            "clientPainPoint": self.client_pain_point,
            "summary": self.summary_en,
            "winningPitch": self.winning_pitch_en,
            "postedAt": self.posted_at.isoformat() if self.posted_at else None,
            "scrapedAt": utc_now().isoformat(),
        }


@dataclass(slots=True)
class Subscriber:
    """A Lemon Squeezy subscription; maps 1:1 to a `subscribers` row."""

    subscription_id: str
    customer_email: str
    status: str = SUBSCRIBER_STATUS_ACTIVE
    telegram_user_id: int | None = None
    invite_link: str | None = None
    updated_at: datetime | None = None

    def to_db_row(self) -> dict[str, Any]:
        """Column values; None fields are omitted so upserts never erase stored data."""
        row: dict[str, Any] = {
            "subscription_id": self.subscription_id,
            "customer_email": self.customer_email.strip().lower(),
            "status": self.status,
        }
        if self.telegram_user_id is not None:
            row["telegram_user_id"] = int(self.telegram_user_id)
        if self.invite_link is not None:
            row["invite_link"] = self.invite_link
        return row

    @classmethod
    def from_db_row(cls, row: Mapping[str, Any]) -> "Subscriber":
        return cls(
            subscription_id=str(row["subscription_id"]),
            customer_email=str(row.get("customer_email") or ""),
            status=str(row.get("status") or SUBSCRIBER_STATUS_ACTIVE),
            telegram_user_id=_coerce_int_or_none(row.get("telegram_user_id")),
            invite_link=row.get("invite_link") or None,
            updated_at=parse_timestamp(row.get("updated_at")),
        )
