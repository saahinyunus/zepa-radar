"""Central configuration: environment variables, source URLs, filters and Topic IDs.

Everything that is tunable lives here so the scrapers, the AI analyzer, the
notifier and the Apify wrapper share one source of truth (dual-engine design).
"""
from __future__ import annotations

import logging
import os
import random
import re

from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------
def _env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    value = _env_str(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = _env_str(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_optional_int(name: str) -> int | None:
    value = _env_str(name)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Execution mode
# ---------------------------------------------------------------------------
MODE_TELEGRAM_SAAS = "TELEGRAM_SAAS"
MODE_APIFY_ACTOR = "APIFY_ACTOR"
VALID_EXECUTION_MODES = (MODE_TELEGRAM_SAAS, MODE_APIFY_ACTOR)

EXECUTION_MODE = _env_str("EXECUTION_MODE", MODE_TELEGRAM_SAAS).upper()

LOG_LEVEL = _env_str("LOG_LEVEL", "INFO").upper()


def configure_logging() -> None:
    """Configure root logging once.

    httpx logs every request URL at INFO level; the Telegram Bot API URL embeds
    the bot token, so httpx/httpcore are pinned to WARNING to keep secrets out
    of public CI logs.
    """
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Secrets / service credentials
# ---------------------------------------------------------------------------
GROQ_API_KEY = _env_str("GROQ_API_KEY")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
# llama-3.3-70b-versatile / llama-3.1-8b-instant (the original spec models) are no
# longer served by Groq, so the defaults are the gpt-oss models. List the models your
# key can use with:  GET https://api.groq.com/openai/v1/models
GROQ_MODEL = _env_str("GROQ_MODEL", "openai/gpt-oss-120b")
# gpt-oss models "think" before answering; low effort keeps the reasoning tokens from
# eating the small output budget. Ignored for other models. Empty = do not send.
GROQ_REASONING_EFFORT = _env_str("GROQ_REASONING_EFFORT", "low")
# Used only when the primary model's quota is exhausted (set to "none" to disable).
# Empty/unset keeps the default because GitHub Actions passes unset secrets as "".
GROQ_FALLBACK_MODEL = _env_str("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b")
if GROQ_FALLBACK_MODEL.lower() in {"none", "off", "disabled"}:
    GROQ_FALLBACK_MODEL = ""
# Stage 1 of the two-stage analysis: a cheap model rejects obvious non-leads so the
# 70B model (small free daily token quota) only sees promising posts.
# Set GROQ_TRIAGE_MODEL=none to disable triage and send everything to GROQ_MODEL.
GROQ_TRIAGE_MODEL = _env_str("GROQ_TRIAGE_MODEL", "openai/gpt-oss-20b")
if GROQ_TRIAGE_MODEL.lower() in {"none", "off", "disabled"}:
    GROQ_TRIAGE_MODEL = ""
TRIAGE_MIN_SCORE = _env_int("TRIAGE_MIN_SCORE", 45)
TRIAGE_MAX_INPUT_CHARS = 1200
TRIAGE_MAX_OUTPUT_TOKENS = 250
# Groq free tier (measured): 1000 requests/day and 8000 tokens/minute per model. A 429 that
# clears within a minute is the token-per-minute limit: wait for it. A longer wait means
# the daily request quota is gone.
GROQ_MAX_WAIT_SECONDS = 65.0
GROQ_MAX_OUTPUT_TOKENS = 1200
AI_MAX_INPUT_CHARS = 2500

SUPABASE_URL = _env_str("SUPABASE_URL")
SUPABASE_KEY = _env_str("SUPABASE_KEY")
SQLITE_PATH = _env_str("SQLITE_PATH", "radar_state.db")

TELEGRAM_BOT_TOKEN = _env_str("TELEGRAM_BOT_TOKEN")
TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_MAX_MESSAGE_CHARS = 4096
TELEGRAM_SEND_INTERVAL_SECONDS = 1.0
TELEGRAM_VIP_GROUP_ID = _env_str("TELEGRAM_VIP_GROUP_ID")
TELEGRAM_FREE_CHANNEL_ID = _env_str("TELEGRAM_FREE_CHANNEL_ID")

TELEGRAM_WEBHOOK_SECRET = _env_str("TELEGRAM_WEBHOOK_SECRET")
TELEGRAM_BOT_USERNAME = _env_str("TELEGRAM_BOT_USERNAME").lstrip("@")
# Your own Telegram user ID (message @userinfobot to find it): health alerts go there. Optional.
TELEGRAM_ADMIN_CHAT_ID = _env_str("TELEGRAM_ADMIN_CHAT_ID")
INVITE_LINK_TTL_HOURS = _env_int("INVITE_LINK_TTL_HOURS", 72)

LEMON_SQUEEZY_WEBHOOK_SECRET = _env_str("LEMON_SQUEEZY_WEBHOOK_SECRET")
LEMON_SQUEEZY_CHECKOUT_URL = _env_str("LEMON_SQUEEZY_CHECKOUT_URL")
# Lemon Squeezy test-mode events (fake card purchases) are ignored unless this is switched on, so a
# live store can never hand out free VIP access to someone paying with a test card.
LEMON_SQUEEZY_ALLOW_TEST_MODE = os.getenv("LEMON_SQUEEZY_ALLOW_TEST_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Runtime tuning
# ---------------------------------------------------------------------------
# A ready-made pitch would reach every VIP member verbatim, so the Telegram product leaves it off.
GENERATE_PITCH = os.getenv("GENERATE_PITCH", "false").strip().lower() in {"1", "true", "yes", "on"}
# Scoring v2 (src/scoring.py) sums to at most ~90, so the publication threshold is lower than with v1.
# Provisional: re-check against labelled leads once enough stored facts exist.
VIP_MIN_SCORE = _env_int("VIP_MIN_SCORE", 60)
# Best-scoring leads go first; the rest wait for the next run (they stay eligible for VIP_RETRY_MAX_AGE_HOURS).
VIP_MAX_PER_RUN = _env_int("VIP_MAX_PER_RUN", 4)
# At most this many leads are posted per UTC day, independent of how often the workflow runs.
# Fewer strong leads beat a full quota: nothing is posted just to fill it. 0 disables the cap.
VIP_DAILY_CAP = _env_int("VIP_DAILY_CAP", 20)
FREE_DELAY_HOURS = _env_float("FREE_DELAY_HOURS", 4.0)
# Set FREE_CHANNEL_ENABLED=false to pause the delayed Free-channel teasers (e.g. until the
# live checkout link exists). VIP delivery is not affected.
FREE_CHANNEL_ENABLED = _env_str("FREE_CHANNEL_ENABLED", "true").lower() not in {"0", "false", "no", "off"}
# Failed VIP deliveries are retried only while the lead is still fresh.
VIP_RETRY_MAX_AGE_HOURS = 6.0

# Only leads posted within this window are analysed. Hiring posts stay open for hours, and a
# wide window lets a run pick up what an earlier one missed (Reddit 429s, GitHub's 35-45 min
# schedule drift); already stored leads are skipped, and the scorer rewards fresher posts.
LEAD_MAX_AGE_MINUTES = _env_int("LEAD_MAX_AGE_MINUTES", 360)
# Groq free tier guard: hard cap on LLM calls in a single run.
MAX_AI_CALLS_PER_RUN = _env_int("MAX_AI_CALLS_PER_RUN", 40)
GROQ_CONCURRENCY = max(1, _env_int("GROQ_CONCURRENCY", 3))
# Soft wall-clock budget for one TELEGRAM_SAAS run. The repo is public (unlimited Actions
# minutes), so runs may wait out Groq's per-minute token limit.
RUN_TIME_BUDGET_SECONDS = _env_int("RUN_TIME_BUDGET_SECONDS", 190)
# Old rows are purged to stay inside the Supabase free tier.
RETENTION_DAYS = _env_int("RETENTION_DAYS", 30)

HTTP_TIMEOUT_SECONDS = _env_float("HTTP_TIMEOUT_SECONDS", 15.0)
HTTP_MAX_RETRIES = max(1, _env_int("HTTP_MAX_RETRIES", 3))
HTTP_BACKOFF_BASE_SECONDS = 1.0
HTTP_BACKOFF_MAX_SECONDS = 20.0
RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


def backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter for a zero-based retry attempt."""
    delay = min(HTTP_BACKOFF_MAX_SECONDS, HTTP_BACKOFF_BASE_SECONDS * (2 ** attempt))
    return delay + random.uniform(0.0, 0.25)


# ---------------------------------------------------------------------------
# Categories and Telegram topics
# ---------------------------------------------------------------------------
CATEGORY_SOFTWARE = "SOFTWARE"
CATEGORY_DESIGN = "DESIGN"
CATEGORY_VIDEO = "VIDEO"
CATEGORY_MARKETING_WRITING = "MARKETING_WRITING"
CATEGORY_OTHER = "OTHER"

CATEGORIES = (
    CATEGORY_SOFTWARE,
    CATEGORY_DESIGN,
    CATEGORY_VIDEO,
    CATEGORY_MARKETING_WRITING,
)
ALL_CATEGORIES = CATEGORIES + (CATEGORY_OTHER,)

CATEGORY_DISPLAY_NAMES = {
    CATEGORY_SOFTWARE: "SOFTWARE",
    CATEGORY_DESIGN: "DESIGN",
    CATEGORY_VIDEO: "VIDEO",
    CATEGORY_MARKETING_WRITING: "MARKETING & WRITING",
    CATEGORY_OTHER: "OTHER",
}

CATEGORY_TOPIC_NAMES = {
    CATEGORY_SOFTWARE: "#💻-software-dev",
    CATEGORY_DESIGN: "#🎨-graphic-design",
    CATEGORY_VIDEO: "#🎬-video-editing",
    CATEGORY_MARKETING_WRITING: "#✍️-copywriting-seo",
}

TOPIC_ID_SOFTWARE = _env_optional_int("TOPIC_ID_SOFTWARE")
TOPIC_ID_DESIGN = _env_optional_int("TOPIC_ID_DESIGN")
TOPIC_ID_VIDEO = _env_optional_int("TOPIC_ID_VIDEO")
TOPIC_ID_MARKETING = _env_optional_int("TOPIC_ID_MARKETING")

CATEGORY_TOPIC_IDS: dict[str, int | None] = {
    CATEGORY_SOFTWARE: TOPIC_ID_SOFTWARE,
    CATEGORY_DESIGN: TOPIC_ID_DESIGN,
    CATEGORY_VIDEO: TOPIC_ID_VIDEO,
    CATEGORY_MARKETING_WRITING: TOPIC_ID_MARKETING,
}


def get_topic_id(category: str) -> int | None:
    """Return the VIP supergroup `message_thread_id` for a category (None if unset)."""
    return CATEGORY_TOPIC_IDS.get(category)


# Keyword hints used by job-board scrapers to guess a category before the LLM runs.
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    CATEGORY_SOFTWARE: (
        "developer", "engineer", "programmer", "software", "backend", "frontend",
        "front-end", "back-end", "full stack", "fullstack", "devops", "python",
        "javascript", "typescript", "react", "node", "django", "laravel", "php",
        "wordpress", "shopify", "mobile app", "ios", "android", "flutter",
        "automation", "zapier", "make.com", "no-code", "nocode", "ai", "machine learning",
        "data scientist", "api", "web developer", "saas", "chatbot",
    ),
    CATEGORY_DESIGN: (
        "designer", "design", "ui/ux", "ux", "ui", "graphic", "logo", "branding",
        "illustrator", "illustration", "figma", "thumbnail", "3d", "photoshop", "creative director",
    ),
    CATEGORY_VIDEO: (
        "video editor", "video editing", "editor", "motion graphic", "motion design",
        "after effects", "premiere", "vfx", "shorts", "reels", "tiktok", "youtube",
        "animator", "animation", "videographer",
    ),
    CATEGORY_MARKETING_WRITING: (
        "copywriter", "copywriting", "content writer", "writer", "seo", "marketing",
        "social media", "growth marketing", "growth marketer", "content strategist", "email marketing", "ads",
        "ppc", "community manager", "blogger", "editor-in-chief", "ghostwriter",
    ),
}


# ---------------------------------------------------------------------------
# Reddit (multi-subreddit groups, joined with "+", ten subs per request)
# ---------------------------------------------------------------------------
REDDIT_URL_TEMPLATE = "https://www.reddit.com/r/{group}/new.json?limit=100"
REDDIT_USER_AGENT = _env_str(
    "REDDIT_USER_AGENT",
    "python:global-freelance-radar:v1.0 (lead discovery bot)",
)

# Reddit throttles aggressively, so a scheduled run fetches only a random subset of the
# groups. (A time-based rotation can alias with GitHub's irregular cron timing and starve
# the same groups, so the choice is random.) 0 = fetch every group on every run.
REDDIT_GROUPS_PER_RUN = max(0, _env_int("REDDIT_GROUPS_PER_RUN", 3))
# Groups fetched on every run (they count towards REDDIT_GROUPS_PER_RUN).
REDDIT_ALWAYS_GROUPS = ("direct_jobs",)

REDDIT_SUBREDDIT_GROUPS: dict[str, tuple[str, ...]] = {
    # Group 1 - direct job postings
    "direct_jobs": (
        "forhire", "DesignJobs", "hireaneditor", "CreatorServices", "VideoEditing",
        "hungryartists", "DoneDirtCheap", "slavelabour", "jobbit", "Jobs4Bitcoins",
        # Added after a probe: real hiring posts. One combined feed request covers all of them.
        "hiring", "gameDevJobs", "gamedevclassifieds", "RemoteJobs", "SEOjobs", "PythonJobs", "WebDevJobs",
        "HireaWriter",
    ),
    # Group 2 - software / SaaS / small-business clients
    "software_saas": (
        "webdev", "SaaS", "startups", "entrepreneur", "smallbusiness",
        "shopify", "wordpress", "nocode", "Automate", "ecommerce",
    ),
    # Group 3 - creators and agencies
    "creators_agencies": (
        "NewTubers", "youtubers", "Twitch", "podcasting", "marketing",
        "SEO", "copywriting", "graphic_design", "content_marketing", "socialmedia",
    ),
    # Group 4 - founder / growth communities (extra coverage)
    "founders_growth": (
        "indiehackers", "SideProject", "microsaas", "DigitalMarketing", "PPC",
        "bigseo", "Entrepreneurship", "startup", "GrowthHacking", "EntrepreneurRideAlong",
    ),
}

REDDIT_GROUPS: dict[str, str] = {
    name: "+".join(subs) for name, subs in REDDIT_SUBREDDIT_GROUPS.items()
}
REDDIT_URLS: dict[str, str] = {
    name: REDDIT_URL_TEMPLATE.format(group=joined) for name, joined in REDDIT_GROUPS.items()
}


# ---------------------------------------------------------------------------
# Hacker News (Algolia)
# ---------------------------------------------------------------------------
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
# Algolia has no `OR` operator (a literal "x OR y" query matches nothing), so the
# three search phrases are sent as separate requests and merged.
HN_QUERIES = ('"looking for"', "hiring", "freelancer")
HN_TAGS = "(story,comment)"
HN_HITS_PER_PAGE = 100
HN_ITEM_URL_TEMPLATE = "https://news.ycombinator.com/item?id={object_id}"


def build_hn_params(since_timestamp: int, query: str) -> dict[str, str | int]:
    """Query parameters for the Algolia endpoint (httpx URL-encodes them)."""
    return {
        "query": query,
        "tags": HN_TAGS,
        "numericFilters": f"created_at_i>{since_timestamp}",
        "hitsPerPage": HN_HITS_PER_PAGE,
    }


# ---------------------------------------------------------------------------
# Remote job boards (JSON + RSS)
# ---------------------------------------------------------------------------
REMOTEOK_API_URL = "https://remoteok.com/api"
# Jobicy asks for at most one automated pass per hour. Scheduled runs fetch it only during
# the first HOURLY_BOARD_MINUTE_WINDOW minutes of an hour (runs are 35+ minutes apart).
JOBICY_API_URL = "https://jobicy.com/api/v2/remote-jobs?count=50"
HOURLY_BOARD_MINUTE_WINDOW = 20
ARBEITNOW_API_URL = "https://www.arbeitnow.com/api/job-board-api"

WWR_RSS_FEEDS: dict[str, str] = {
    CATEGORY_SOFTWARE: "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    CATEGORY_DESIGN: "https://weworkremotely.com/categories/remote-design-jobs.rss",
    CATEGORY_MARKETING_WRITING: "https://weworkremotely.com/categories/remote-sales-and-marketing-jobs.rss",
}

JOB_BOARD_URLS: dict[str, str] = {
    "remoteok": REMOTEOK_API_URL,
    "jobicy": JOBICY_API_URL,
    "arbeitnow": ARBEITNOW_API_URL,
}

# Source switches: sources whose terms of use are unclear can be turned off without touching code,
# e.g. DISABLED_SOURCES=freelancer. The rest of the pipeline keeps running. See SOURCES.md.
DISABLED_SOURCES = frozenset(
    item.strip().lower() for item in os.getenv("DISABLED_SOURCES", "").split(",") if item.strip()
)
# Job boards to fetch. Arbeitnow lists local German/Dutch employment, not remote freelance work.
ENABLED_JOB_BOARDS = frozenset(
    item.strip().lower()
    for item in os.getenv("ENABLED_JOB_BOARDS", "remoteok,jobicy,weworkremotely").split(",")
    if item.strip()
)

# Himalayas remote-jobs API (free, no key, newest first, 20 per page, cursor paging). Only roles it
# labels contract / part time / temporary are kept; it refreshes about daily, so it is polled rarely.
HIMALAYAS_API_URL = "https://himalayas.app/jobs/api"
HIMALAYAS_MAX_PAGES = _env_int("HIMALAYAS_MAX_PAGES", 4)
HIMALAYAS_POLL_HOURS = max(1, _env_int("HIMALAYAS_POLL_HOURS", 3))
HIMALAYAS_FLEXIBLE_TYPES = frozenset({"contractor", "part time", "temporary"})

# Freelancer.com public projects API (no key). Every project is a client request.
FREELANCER_API_URL = "https://www.freelancer.com/api/projects/0.1/projects/active/"
FREELANCER_FETCH_LIMIT = 100
FREELANCER_MAX_PER_RUN = _env_int("FREELANCER_MAX_PER_RUN", 10)
# Budgets are compared in USD (the API gives an exchange rate per currency).
FREELANCER_MIN_BUDGET_USD = _env_int("FREELANCER_MIN_BUDGET_USD", 150)
FREELANCER_MIN_HOURLY_USD = _env_int("FREELANCER_MIN_HOURLY_USD", 12)

# Sources whose every entry is a hiring post (no "for hire" noise).
TRUSTED_HIRING_SOURCES = frozenset(
    {"remoteok", "jobicy", "arbeitnow", "weworkremotely", "freelancer", "himalayas"}
)


# ---------------------------------------------------------------------------
# HTTP client defaults
# ---------------------------------------------------------------------------
USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
)


# ---------------------------------------------------------------------------
# Pre-filter (runs BEFORE the LLM to save Groq quota)
# ---------------------------------------------------------------------------
# A post must voice hiring intent in its title or opening lines. Loose words such as "$", "paid"
# or "project" appear in nearly every startup discussion and only wasted LLM quota.
HIRING_INTENT = re.compile(
    r"\[hiring\]|\bhiring\b|\blooking (?:for|to hire)\b|\b(?:need|needs|needed)\s+(?:a|an|some|someone|help)\b"
    r"|\bwant(?:ed)? to hire\b|\bhire (?:a|an|someone)\b|\bseeking (?:a|an)\b"
    r"|\bpaid (?:gig|project|work|task)s?\b|\bbudget\b|\bwho can (?:build|design|edit|write|make)\b"
    r"|\b(?:developer|designer|editor|writer|freelancer)s? wanted\b|\brecommend (?:a|an|any)\b",
    re.IGNORECASE,
)
# Only the title and this many characters of the body are checked: long discussion posts
# mention budgets and projects in passing.
HIRING_INTENT_BODY_CHARS = 400

# Posts whose title starts with one of these are freelancers advertising
# themselves, not clients hiring - dropped immediately.
NEGATIVE_PREFIXES = (
    "[for hire]",
    "(for hire)",
    "for hire:",
    "[forhire]",
    "i will edit",
    "i will design",
    "i will write",
    "i can design",
    "i can edit",
    "i can write",
    "i will build",
    "i can build",
    "i will create",
    "i can create",
    "[offer]",
    "(offer)",
    "available for hire",
    "offering my services",
    "my portfolio",
)

MIN_TEXT_LENGTH = 20


def passes_prefilter(title: str, body: str = "", *, trusted_source: bool = False) -> bool:
    """Cheap rule-based gate applied before any LLM call.

    * Titles starting with a NEGATIVE_PREFIXES entry are rejected outright.
    * Entries from `trusted_source` boards (every item is a hiring post) pass
      without needing a positive signal.
    * Everything else must voice hiring intent (HIRING_INTENT) in the title or opening lines.
    """
    clean_title = (title or "").strip().lower().lstrip("*-_>~ \t")
    if len(clean_title) < MIN_TEXT_LENGTH // 2:
        return False
    if clean_title.startswith(NEGATIVE_PREFIXES):
        return False
    if trusted_source:
        return True
    opening = f"{clean_title}\n{(body or '')[:HIRING_INTENT_BODY_CHARS]}"
    return HIRING_INTENT.search(opening) is not None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_config(mode: str | None = None) -> list[str]:
    """Return a list of human-readable configuration problems (empty == OK)."""
    mode = (mode or EXECUTION_MODE).upper()
    problems: list[str] = []

    if mode not in VALID_EXECUTION_MODES:
        problems.append(
            f"EXECUTION_MODE must be one of {', '.join(VALID_EXECUTION_MODES)} (got '{mode}')."
        )
        return problems

    if not GROQ_API_KEY:
        problems.append("GROQ_API_KEY is not set.")

    if mode == MODE_TELEGRAM_SAAS:
        if bool(SUPABASE_URL) != bool(SUPABASE_KEY):
            problems.append("SUPABASE_URL and SUPABASE_KEY must both be set (or both empty).")
        if not TELEGRAM_BOT_TOKEN:
            problems.append("TELEGRAM_BOT_TOKEN is not set.")
        if not TELEGRAM_VIP_GROUP_ID:
            problems.append("TELEGRAM_VIP_GROUP_ID is not set.")
        if FREE_CHANNEL_ENABLED:
            if not TELEGRAM_FREE_CHANNEL_ID:
                problems.append("TELEGRAM_FREE_CHANNEL_ID is not set.")
            if not LEMON_SQUEEZY_CHECKOUT_URL:
                problems.append("LEMON_SQUEEZY_CHECKOUT_URL is not set (needed for the Free-channel button).")
        for category, topic_id in CATEGORY_TOPIC_IDS.items():
            if topic_id is None:
                problems.append(f"Topic ID for {category} is not set (TOPIC_ID_* variables).")

    return problems
