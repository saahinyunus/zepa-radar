"""TELEGRAM_SAAS orchestrator - one radar cycle (run by GitHub Actions every 20 minutes).

Cycle: scrape -> deduplicate -> AI analysis -> store -> VIP delivery ->
delayed Free-channel teasers -> housekeeping.

    python main.py              # production cycle
    python main.py --dry-run    # scrape + analyse, print results, no DB writes / Telegram sends
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass

from src import config
from src.ai_analyzer import AIAnalyzer
from src.database import BaseDatabase, DatabaseError, create_database
from src.models import AnalyzedLead, utc_now
from src.scrapers import scrape_all
from src.telegram_notifier import TelegramNotifier, format_vip_message

logger = logging.getLogger("radar")

# AI always gets at least this long after scraping, even if scraping was slow.
MIN_AI_WINDOW_SECONDS = 10.0
VIP_BATCH_LIMIT = 20
FREE_BATCH_LIMIT = 20
# Old rows are purged once per hour (during the first 20 minutes) to save API calls.
CLEANUP_MINUTE_WINDOW = 20


@dataclass(slots=True)
class RunStats:
    scraped: int = 0
    new: int = 0
    analyzed: int = 0
    saved: int = 0
    vip_candidates: int = 0
    vip_sent: int = 0
    free_sent: int = 0
    purged: int = 0
    errors: int = 0

    def summary(self) -> str:
        return (
            f"scraped={self.scraped} new={self.new} analyzed={self.analyzed} saved={self.saved} "
            f"vip_candidates={self.vip_candidates} vip_sent={self.vip_sent} "
            f"free_sent={self.free_sent} purged={self.purged} errors={self.errors}"
        )


async def run_cycle(
    db: BaseDatabase,
    analyzer: AIAnalyzer,
    notifier: TelegramNotifier,
    *,
    dry_run: bool = False,
) -> RunStats:
    """Execute one full radar cycle. A failing phase is logged and the next phase still runs."""
    started = time.monotonic()
    stats = RunStats()
    analyzed: list[AnalyzedLead] = []

    # 1-3. scrape, deduplicate, analyse
    try:
        raw_leads = await scrape_all(rotate_reddit=True)
        stats.scraped = len(raw_leads)

        new_ids = await db.filter_new_ids([lead.item_id for lead in raw_leads])
        new_leads = [lead for lead in raw_leads if lead.item_id in new_ids]
        stats.new = len(new_leads)
        logger.info("%d scraped lead(s), %d new", stats.scraped, stats.new)

        if new_leads:
            deadline = max(
                started + config.RUN_TIME_BUDGET_SECONDS,
                time.monotonic() + MIN_AI_WINDOW_SECONDS,
            )
            analyzed = await analyzer.analyze_many(new_leads, deadline=deadline)
            stats.analyzed = len(analyzed)
    except Exception:  # noqa: BLE001 - keep delivering backlog even if scraping/AI breaks
        stats.errors += 1
        logger.exception("Scrape/analyse phase failed")

    # 4. persist every analysed lead (rejects too, so they are never re-analysed)
    if dry_run:
        _print_dry_run(analyzed)
    else:
        for lead in analyzed:
            try:
                if await db.save_lead(lead):
                    stats.saved += 1
            except DatabaseError:
                stats.errors += 1
                logger.exception("Could not save %s", lead.item_id)

    if dry_run:
        return stats

    # 5. VIP: new leads plus any earlier lead whose delivery failed
    try:
        pending = await db.get_pending_vip_leads(limit=VIP_BATCH_LIMIT)
        stats.vip_candidates = len(pending)
        if pending:
            stats.vip_sent = await notifier.send_vip_leads(pending, db)
    except Exception:  # noqa: BLE001
        stats.errors += 1
        logger.exception("VIP delivery phase failed")

    # 6. Free channel teasers for leads older than FREE_DELAY_HOURS
    if config.FREE_CHANNEL_ENABLED:
        try:
            due = await db.get_due_free_leads(limit=FREE_BATCH_LIMIT)
            if due:
                stats.free_sent = await notifier.send_free_leads(due, db)
        except Exception:  # noqa: BLE001
            stats.errors += 1
            logger.exception("Free channel phase failed")
    else:
        logger.info("Free channel delivery is paused (FREE_CHANNEL_ENABLED=false)")

    # 7. housekeeping
    if utc_now().minute < CLEANUP_MINUTE_WINDOW:
        try:
            stats.purged = await db.cleanup_old_leads(config.RETENTION_DAYS)
        except Exception:  # noqa: BLE001
            stats.errors += 1
            logger.exception("Cleanup failed")

    logger.info("Cycle finished in %.1fs | %s", time.monotonic() - started, stats.summary())
    return stats


def _print_dry_run(leads: list[AnalyzedLead]) -> None:
    print(f"\n=== DRY RUN: {len(leads)} analysed lead(s) ===")
    for lead in leads:
        flag = "VIP " if lead.is_vip_worthy() else "skip"
        print(f"[{flag}] {lead.quality_score:>3} {lead.category:<18} {lead.source:<14} {lead.title[:80]}")
    for lead in leads:
        if lead.is_vip_worthy():
            print("\n--- VIP message preview ---")
            print(format_vip_message(lead))
            print(f"URL: {lead.url}")


async def amain(dry_run: bool) -> int:
    problems = config.validate_config(config.MODE_TELEGRAM_SAAS)
    if dry_run:
        problems = [problem for problem in problems if problem.startswith("GROQ")]
    if problems:
        for problem in problems:
            logger.error("Config error: %s", problem)
        return 1

    if os.getenv("GITHUB_ACTIONS") and not (config.SUPABASE_URL and config.SUPABASE_KEY):
        logger.warning(
            "Running on GitHub Actions with local SQLite: state is lost after every run, "
            "so leads will be re-sent. Configure SUPABASE_URL / SUPABASE_KEY."
        )

    try:
        async with create_database() as db, AIAnalyzer() as analyzer, TelegramNotifier() as notifier:
            stats = await run_cycle(db, analyzer, notifier, dry_run=dry_run)
    except DatabaseError:
        logger.exception("Database unavailable - aborting cycle")
        return 1

    return 1 if stats.errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Global Freelance & B2B Lead Radar (Telegram SaaS mode)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="scrape and analyse only; do not write to the database or send Telegram messages",
    )
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    config.configure_logging()

    return asyncio.run(amain(args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
