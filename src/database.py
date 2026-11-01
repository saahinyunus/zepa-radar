"""Deduplication and subscriber storage with automatic backend selection.

* `SUPABASE_URL` + `SUPABASE_KEY` set  -> Supabase (PostgREST over httpx).
* otherwise                            -> local SQLite file (`radar_state.db`).

Both backends expose the same async interface (`BaseDatabase`). Create one with
`create_database()` and use it as an async context manager:

    async with create_database() as db:
        new_ids = await db.filter_new_ids(ids)

Print the Supabase table DDL with:  python -m src.database
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from abc import ABC, abstractmethod
from contextlib import closing
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Iterator, Sequence, TypeVar

import httpx

from src import config
from src.models import (
    SUBSCRIBER_STATUSES,
    AnalyzedLead,
    Subscriber,
    SupportMessage,
    SupportTicket,
    format_db_timestamp,
    load_facts,
    parse_timestamp,
    utc_now,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

LEADS_TABLE = "processed_leads"
SUBSCRIBERS_TABLE = "subscribers"
SUPPORT_TICKETS_TABLE = "support_tickets"
SUPPORT_MESSAGES_TABLE = "support_messages"
SUPPORT_BLOCKED_TABLE = "support_blocked"

SQLITE_ID_CHUNK = 500
SUPABASE_ID_CHUNK = 50

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
SQLITE_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS processed_leads (
        item_id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        category TEXT NOT NULL,
        title TEXT NOT NULL,
        url TEXT NOT NULL,
        quality_score INTEGER NOT NULL,
        estimated_budget TEXT,
        summary_en TEXT,
        pitch_template_en TEXT,
        client_pain_point TEXT,
        sent_to_vip INTEGER DEFAULT 0,
        sent_to_free INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS subscribers (
        subscription_id TEXT PRIMARY KEY,
        customer_email TEXT NOT NULL,
        telegram_user_id BIGINT,
        status TEXT NOT NULL,
        invite_link TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_leads_created_at ON processed_leads (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_leads_delivery ON processed_leads (sent_to_vip, sent_to_free, quality_score)",
    "CREATE INDEX IF NOT EXISTS idx_subscribers_email ON subscribers (customer_email)",
    "CREATE INDEX IF NOT EXISTS idx_subscribers_invite ON subscribers (invite_link)",
    """
    CREATE TABLE IF NOT EXISTS support_tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_user_id BIGINT NOT NULL,
        name TEXT,
        username TEXT,
        status TEXT NOT NULL DEFAULT 'open',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS support_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER,
        telegram_user_id BIGINT NOT NULL,
        direction TEXT NOT NULL,
        body TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS support_blocked (
        telegram_user_id BIGINT PRIMARY KEY,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_support_tickets_user ON support_tickets (telegram_user_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_support_messages_user ON support_messages (telegram_user_id, created_at)",
)

# The support desk tables are optional: the support bot still forwards messages without them.
SUPABASE_SUPPORT_SQL = """\
-- Support desk (tickets, history, blocking). Paste into Supabase -> SQL Editor -> Run (once).
CREATE TABLE IF NOT EXISTS support_tickets (
    id BIGSERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL,
    name TEXT,
    username TEXT,
    status TEXT NOT NULL DEFAULT 'open', -- 'open', 'closed'
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS support_messages (
    id BIGSERIAL PRIMARY KEY,
    ticket_id BIGINT,
    telegram_user_id BIGINT NOT NULL,
    direction TEXT NOT NULL, -- 'customer', 'operator', 'auto'
    body TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS support_blocked (
    telegram_user_id BIGINT PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_support_tickets_user ON support_tickets (telegram_user_id, status);
CREATE INDEX IF NOT EXISTS idx_support_messages_user ON support_messages (telegram_user_id, created_at);

ALTER TABLE support_tickets ENABLE ROW LEVEL SECURITY;
ALTER TABLE support_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE support_blocked ENABLE ROW LEVEL SECURITY;
"""

SUPABASE_SCHEMA_SQL = """\
-- Paste this into Supabase -> SQL Editor -> Run (once).
CREATE TABLE IF NOT EXISTS processed_leads (
    item_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    quality_score INTEGER NOT NULL,
    estimated_budget TEXT,
    summary_en TEXT,
    pitch_template_en TEXT,
    client_pain_point TEXT,
    sent_to_vip INTEGER DEFAULT 0,
    sent_to_free INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS subscribers (
    subscription_id TEXT PRIMARY KEY,
    customer_email TEXT NOT NULL,
    telegram_user_id BIGINT,
    status TEXT NOT NULL, -- 'active', 'cancelled', 'expired'
    invite_link TEXT,
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_leads_created_at ON processed_leads (created_at);
CREATE INDEX IF NOT EXISTS idx_leads_delivery ON processed_leads (sent_to_vip, sent_to_free, quality_score);
CREATE INDEX IF NOT EXISTS idx_subscribers_email ON subscribers (customer_email);
CREATE INDEX IF NOT EXISTS idx_subscribers_invite ON subscribers (invite_link);

-- Block the public anon key; the service_role key used by the bot bypasses RLS.
ALTER TABLE processed_leads ENABLE ROW LEVEL SECURITY;
ALTER TABLE subscribers ENABLE ROW LEVEL SECURITY;

""" + SUPABASE_SUPPORT_SQL



def stamp_sent_at(stored: str, when: datetime) -> str | None:
    """The facts JSON in `pitch_template_en` with `sent_at` added; None when the row holds no facts."""
    facts = load_facts(stored or "")
    if not facts:
        return None
    facts["sent_at"] = when.isoformat()
    return json.dumps(facts, separators=(",", ":"))


def effective_sent_time(created_at: Any, stored: str) -> datetime | None:
    """When a VIP lead was delivered: the stored `sent_at`, else (older rows) its capture time."""
    return parse_timestamp(load_facts(stored or "").get("sent_at")) or parse_timestamp(created_at)


class DatabaseError(Exception):
    """Raised when a database operation fails after all retries."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _chunks(items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))


def _validate_status(status: str) -> str:
    normalized = (status or "").strip().lower()
    if normalized not in SUBSCRIBER_STATUSES:
        raise ValueError(f"Invalid subscriber status '{status}'. Expected one of {SUBSCRIBER_STATUSES}.")
    return normalized


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------
class BaseDatabase(ABC):
    """Backend-agnostic storage interface used by main.py, the notifier and the webhook."""

    backend_name = "base"

    async def __aenter__(self) -> "BaseDatabase":
        await self.init()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    @abstractmethod
    async def init(self) -> None:
        """Open connections and make sure the tables exist."""
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        """Release connections."""
        raise NotImplementedError

    # --- leads -------------------------------------------------------------
    @abstractmethod
    async def filter_new_ids(self, item_ids: Sequence[str]) -> set[str]:
        """Return the subset of `item_ids` that is not stored yet."""
        raise NotImplementedError

    @abstractmethod
    async def save_lead(self, lead: AnalyzedLead) -> bool:
        """Insert a lead; returns False if its item_id already exists."""
        raise NotImplementedError

    @abstractmethod
    async def get_pending_vip_leads(self, limit: int = 20) -> list[AnalyzedLead]:
        """Fresh leads with score >= VIP_MIN_SCORE that still wait for VIP delivery."""
        raise NotImplementedError

    @abstractmethod
    async def count_vip_sent_since(self, since: datetime) -> int:
        """Number of leads captured at/after `since` that were already delivered to the VIP group."""
        raise NotImplementedError

    @abstractmethod
    async def get_due_free_leads(self, limit: int = 20) -> list[AnalyzedLead]:
        """VIP-delivered leads aged FREE_DELAY_HOURS..FREE_MAX_AGE_HOURS that are not yet in the Free channel."""
        raise NotImplementedError

    @abstractmethod
    async def mark_sent_vip(self, item_id: str) -> None:
        """Flag a lead as delivered to the VIP group."""
        raise NotImplementedError

    @abstractmethod
    async def mark_sent_free(self, item_id: str) -> None:
        """Flag a lead as delivered to the Free channel."""
        raise NotImplementedError

    @abstractmethod
    async def cleanup_old_leads(self, days: int) -> int:
        """Delete leads older than `days`; returns the number of removed rows."""
        raise NotImplementedError

    # --- subscribers -------------------------------------------------------
    @abstractmethod
    async def upsert_subscriber(self, subscriber: Subscriber) -> None:
        """Insert or update a subscription (None fields never overwrite stored values)."""
        raise NotImplementedError

    @abstractmethod
    async def get_subscriber(self, subscription_id: str) -> Subscriber | None:
        """Look up a subscription by Lemon Squeezy subscription id."""
        raise NotImplementedError

    @abstractmethod
    async def get_subscribers_by_email(self, email: str) -> list[Subscriber]:
        """All subscriptions belonging to an e-mail address."""
        raise NotImplementedError

    @abstractmethod
    async def get_subscriber_by_invite_link(self, invite_link: str) -> Subscriber | None:
        """Find the subscription an invite link was generated for."""
        raise NotImplementedError

    @abstractmethod
    async def get_subscribers_by_telegram_id(self, telegram_user_id: int) -> list[Subscriber]:
        """All subscriptions bound to a Telegram account."""
        raise NotImplementedError

    @abstractmethod
    async def update_subscriber_status(self, subscription_id: str, status: str) -> bool:
        """Change status ('active' | 'cancelled' | 'expired'); False if the id is unknown."""
        raise NotImplementedError

    @abstractmethod
    async def set_telegram_user_id(self, subscription_id: str, telegram_user_id: int) -> bool:
        """Bind a Telegram user to a subscription; False if the id is unknown."""
        raise NotImplementedError

    # --- support desk ------------------------------------------------------
    @abstractmethod
    async def create_support_ticket(self, telegram_user_id: int, name: str, username: str) -> SupportTicket:
        raise NotImplementedError

    @abstractmethod
    async def get_support_ticket(self, ticket_id: int) -> SupportTicket | None:
        raise NotImplementedError

    @abstractmethod
    async def get_open_support_ticket(self, telegram_user_id: int) -> SupportTicket | None:
        """The customer's newest open ticket, if any."""
        raise NotImplementedError

    @abstractmethod
    async def close_support_ticket(self, ticket_id: int) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def list_open_support_tickets(self, limit: int = 20) -> list[SupportTicket]:
        raise NotImplementedError

    @abstractmethod
    async def add_support_message(
        self, ticket_id: int | None, telegram_user_id: int, direction: str, body: str
    ) -> None:
        """Log a conversation line and bump the ticket's `updated_at`."""
        raise NotImplementedError

    @abstractmethod
    async def get_support_messages(self, telegram_user_id: int, limit: int = 15) -> list[SupportMessage]:
        """The customer's latest lines across all tickets, oldest first."""
        raise NotImplementedError

    @abstractmethod
    async def is_support_blocked(self, telegram_user_id: int) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def set_support_blocked(self, telegram_user_id: int, blocked: bool) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------
class SQLiteDatabase(BaseDatabase):
    """Local file backend. Blocking sqlite3 calls run in worker threads."""

    backend_name = "sqlite"

    def __init__(self, path: str) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _run_sync(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        with closing(self._connect()) as conn:
            result = operation(conn)
            conn.commit()
            return result

    async def _run(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        try:
            return await asyncio.to_thread(self._run_sync, operation)
        except sqlite3.Error as exc:
            raise DatabaseError(f"SQLite error: {exc}") from exc

    async def init(self) -> None:
        def operation(conn: sqlite3.Connection) -> None:
            for statement in SQLITE_SCHEMA_STATEMENTS:
                conn.execute(statement)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(processed_leads)")}
            if "client_pain_point" not in columns:
                conn.execute("ALTER TABLE processed_leads ADD COLUMN client_pain_point TEXT")

        await self._run(operation)
        logger.info("SQLite database ready at %s", self.path)

    async def close(self) -> None:
        # Connections are opened per operation, nothing is kept open.
        logger.debug("SQLite database closed")

    # --- leads -------------------------------------------------------------
    async def filter_new_ids(self, item_ids: Sequence[str]) -> set[str]:
        ids = _unique(item_ids)
        if not ids:
            return set()

        def operation(conn: sqlite3.Connection) -> set[str]:
            existing: set[str] = set()
            for chunk in _chunks(ids, SQLITE_ID_CHUNK):
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT item_id FROM processed_leads WHERE item_id IN ({placeholders})",
                    list(chunk),
                ).fetchall()
                existing.update(row["item_id"] for row in rows)
            return {item_id for item_id in ids if item_id not in existing}

        return await self._run(operation)

    async def save_lead(self, lead: AnalyzedLead) -> bool:
        row = lead.to_db_row()
        row["created_at"] = format_db_timestamp(lead.created_at or utc_now())
        columns = ", ".join(row)
        placeholders = ", ".join(f":{name}" for name in row)

        def operation(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                f"INSERT OR IGNORE INTO processed_leads ({columns}) VALUES ({placeholders})", row
            )
            return cursor.rowcount == 1

        return await self._run(operation)

    async def get_pending_vip_leads(self, limit: int = 20) -> list[AnalyzedLead]:
        oldest = format_db_timestamp(utc_now() - timedelta(hours=config.VIP_RETRY_MAX_AGE_HOURS))

        def operation(conn: sqlite3.Connection) -> list[AnalyzedLead]:
            rows = conn.execute(
                "SELECT * FROM processed_leads "
                "WHERE quality_score >= ? AND sent_to_vip = 0 AND created_at >= ? "
                "ORDER BY quality_score DESC, created_at ASC LIMIT ?",
                (config.VIP_MIN_SCORE, oldest, limit),
            ).fetchall()
            return [AnalyzedLead.from_db_row(dict(row)) for row in rows]

        return await self._run(operation)

    async def count_vip_sent_since(self, since: datetime) -> int:
        def operation(conn: sqlite3.Connection) -> int:
            # A lead is delivered at most VIP_RETRY_MAX_AGE_HOURS after capture, so older rows cannot match.
            earliest = since - timedelta(hours=config.VIP_RETRY_MAX_AGE_HOURS + 1)
            rows = conn.execute(
                "SELECT created_at, pitch_template_en FROM processed_leads WHERE sent_to_vip = 1 AND created_at >= ?",
                (format_db_timestamp(earliest),),
            ).fetchall()
            return sum(1 for created, stored in rows if (effective_sent_time(created, stored) or since) >= since)

        return await self._run(operation)

    async def get_due_free_leads(self, limit: int = 20) -> list[AnalyzedLead]:
        cutoff = format_db_timestamp(utc_now() - timedelta(hours=config.FREE_DELAY_HOURS))
        oldest = format_db_timestamp(utc_now() - timedelta(hours=config.FREE_MAX_AGE_HOURS))

        def operation(conn: sqlite3.Connection) -> list[AnalyzedLead]:
            rows = conn.execute(
                "SELECT * FROM processed_leads "
                "WHERE quality_score >= ? AND sent_to_vip = 1 AND sent_to_free = 0 "
                "AND created_at <= ? AND created_at >= ? "
                "ORDER BY created_at ASC LIMIT ?",
                (max(config.VIP_MIN_SCORE, config.FREE_MIN_SCORE), cutoff, oldest, limit),
            ).fetchall()
            return [AnalyzedLead.from_db_row(dict(row)) for row in rows]

        return await self._run(operation)

    async def mark_sent_vip(self, item_id: str) -> None:
        def operation(conn: sqlite3.Connection) -> None:
            row = conn.execute("SELECT pitch_template_en FROM processed_leads WHERE item_id = ?", (item_id,)).fetchone()
            stamped = stamp_sent_at(row[0] if row and row[0] else "", utc_now())
            if stamped is None:
                conn.execute("UPDATE processed_leads SET sent_to_vip = 1 WHERE item_id = ?", (item_id,))
            else:
                conn.execute(
                    "UPDATE processed_leads SET sent_to_vip = 1, pitch_template_en = ? WHERE item_id = ?",
                    (stamped, item_id),
                )

        await self._run(operation)

    async def mark_sent_free(self, item_id: str) -> None:
        def operation(conn: sqlite3.Connection) -> None:
            conn.execute("UPDATE processed_leads SET sent_to_free = 1 WHERE item_id = ?", (item_id,))

        await self._run(operation)

    async def cleanup_old_leads(self, days: int) -> int:
        cutoff = format_db_timestamp(utc_now() - timedelta(days=days))

        def operation(conn: sqlite3.Connection) -> int:
            cursor = conn.execute("DELETE FROM processed_leads WHERE created_at < ?", (cutoff,))
            return max(0, cursor.rowcount)

        return await self._run(operation)

    # --- subscribers -------------------------------------------------------
    async def upsert_subscriber(self, subscriber: Subscriber) -> None:
        row = subscriber.to_db_row()
        row["status"] = _validate_status(row["status"])
        row.setdefault("telegram_user_id", None)
        row.setdefault("invite_link", None)
        row["updated_at"] = format_db_timestamp(utc_now())

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO subscribers
                    (subscription_id, customer_email, telegram_user_id, status, invite_link, updated_at)
                VALUES
                    (:subscription_id, :customer_email, :telegram_user_id, :status, :invite_link, :updated_at)
                ON CONFLICT(subscription_id) DO UPDATE SET
                    customer_email = excluded.customer_email,
                    telegram_user_id = COALESCE(excluded.telegram_user_id, subscribers.telegram_user_id),
                    status = excluded.status,
                    invite_link = COALESCE(excluded.invite_link, subscribers.invite_link),
                    updated_at = excluded.updated_at
                """,
                row,
            )

        await self._run(operation)

    async def get_subscriber(self, subscription_id: str) -> Subscriber | None:
        def operation(conn: sqlite3.Connection) -> Subscriber | None:
            row = conn.execute(
                "SELECT * FROM subscribers WHERE subscription_id = ?", (subscription_id,)
            ).fetchone()
            return Subscriber.from_db_row(dict(row)) if row else None

        return await self._run(operation)

    async def get_subscribers_by_email(self, email: str) -> list[Subscriber]:
        normalized = email.strip().lower()

        def operation(conn: sqlite3.Connection) -> list[Subscriber]:
            rows = conn.execute(
                "SELECT * FROM subscribers WHERE customer_email = ? ORDER BY updated_at DESC",
                (normalized,),
            ).fetchall()
            return [Subscriber.from_db_row(dict(row)) for row in rows]

        return await self._run(operation)

    async def get_subscriber_by_invite_link(self, invite_link: str) -> Subscriber | None:
        def operation(conn: sqlite3.Connection) -> Subscriber | None:
            row = conn.execute(
                "SELECT * FROM subscribers WHERE invite_link = ?", (invite_link,)
            ).fetchone()
            return Subscriber.from_db_row(dict(row)) if row else None

        return await self._run(operation)

    async def get_subscribers_by_telegram_id(self, telegram_user_id: int) -> list[Subscriber]:
        def operation(conn: sqlite3.Connection) -> list[Subscriber]:
            rows = conn.execute(
                "SELECT * FROM subscribers WHERE telegram_user_id = ? ORDER BY updated_at DESC",
                (int(telegram_user_id),),
            ).fetchall()
            return [Subscriber.from_db_row(dict(row)) for row in rows]

        return await self._run(operation)

    async def update_subscriber_status(self, subscription_id: str, status: str) -> bool:
        normalized = _validate_status(status)
        now = format_db_timestamp(utc_now())

        def operation(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "UPDATE subscribers SET status = ?, updated_at = ? WHERE subscription_id = ?",
                (normalized, now, subscription_id),
            )
            return cursor.rowcount > 0

        return await self._run(operation)

    async def set_telegram_user_id(self, subscription_id: str, telegram_user_id: int) -> bool:
        now = format_db_timestamp(utc_now())

        def operation(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "UPDATE subscribers SET telegram_user_id = ?, updated_at = ? WHERE subscription_id = ?",
                (int(telegram_user_id), now, subscription_id),
            )
            return cursor.rowcount > 0

        return await self._run(operation)

    # --- support desk ------------------------------------------------------
    async def create_support_ticket(self, telegram_user_id: int, name: str, username: str) -> SupportTicket:
        now = format_db_timestamp(utc_now())

        def operation(conn: sqlite3.Connection) -> SupportTicket:
            cursor = conn.execute(
                "INSERT INTO support_tickets (telegram_user_id, name, username, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'open', ?, ?)",
                (int(telegram_user_id), name, username, now, now),
            )
            row = conn.execute("SELECT * FROM support_tickets WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return SupportTicket.from_db_row(dict(row))

        return await self._run(operation)

    def _fetch_tickets(self, conn: sqlite3.Connection, where: str, params: tuple, limit: int) -> list[SupportTicket]:
        rows = conn.execute(
            f"SELECT * FROM support_tickets WHERE {where} ORDER BY updated_at DESC, id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [SupportTicket.from_db_row(dict(row)) for row in rows]

    async def get_support_ticket(self, ticket_id: int) -> SupportTicket | None:
        def operation(conn: sqlite3.Connection) -> SupportTicket | None:
            found = self._fetch_tickets(conn, "id = ?", (int(ticket_id),), 1)
            return found[0] if found else None

        return await self._run(operation)

    async def get_open_support_ticket(self, telegram_user_id: int) -> SupportTicket | None:
        def operation(conn: sqlite3.Connection) -> SupportTicket | None:
            found = self._fetch_tickets(conn, "telegram_user_id = ? AND status = 'open'", (int(telegram_user_id),), 1)
            return found[0] if found else None

        return await self._run(operation)

    async def close_support_ticket(self, ticket_id: int) -> bool:
        now = format_db_timestamp(utc_now())

        def operation(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "UPDATE support_tickets SET status = 'closed', updated_at = ? WHERE id = ?", (now, int(ticket_id))
            )
            return cursor.rowcount > 0

        return await self._run(operation)

    async def list_open_support_tickets(self, limit: int = 20) -> list[SupportTicket]:
        return await self._run(lambda conn: self._fetch_tickets(conn, "status = 'open'", (), int(limit)))

    async def add_support_message(
        self, ticket_id: int | None, telegram_user_id: int, direction: str, body: str
    ) -> None:
        now = format_db_timestamp(utc_now())

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO support_messages (ticket_id, telegram_user_id, direction, body, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (ticket_id, int(telegram_user_id), direction, body, now),
            )
            if ticket_id is not None:
                conn.execute("UPDATE support_tickets SET updated_at = ? WHERE id = ?", (now, int(ticket_id)))

        await self._run(operation)

    async def get_support_messages(self, telegram_user_id: int, limit: int = 15) -> list[SupportMessage]:
        def operation(conn: sqlite3.Connection) -> list[SupportMessage]:
            rows = conn.execute(
                "SELECT * FROM support_messages WHERE telegram_user_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                (int(telegram_user_id), int(limit)),
            ).fetchall()
            return [SupportMessage.from_db_row(dict(row)) for row in reversed(rows)]

        return await self._run(operation)

    async def is_support_blocked(self, telegram_user_id: int) -> bool:
        def operation(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                "SELECT 1 FROM support_blocked WHERE telegram_user_id = ?", (int(telegram_user_id),)
            ).fetchone()
            return row is not None

        return await self._run(operation)

    async def set_support_blocked(self, telegram_user_id: int, blocked: bool) -> None:
        now = format_db_timestamp(utc_now())

        def operation(conn: sqlite3.Connection) -> None:
            if blocked:
                conn.execute(
                    "INSERT OR IGNORE INTO support_blocked (telegram_user_id, created_at) VALUES (?, ?)",
                    (int(telegram_user_id), now),
                )
            else:
                conn.execute("DELETE FROM support_blocked WHERE telegram_user_id = ?", (int(telegram_user_id),))

        await self._run(operation)


# ---------------------------------------------------------------------------
# Supabase backend (PostgREST)
# ---------------------------------------------------------------------------
def _quote_list_value(value: str) -> str:
    """Quote a value for a PostgREST `in.(...)` filter."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _retry_after_seconds(response: httpx.Response) -> float | None:
    header = response.headers.get("retry-after")
    if not header:
        return None
    try:
        return max(0.0, min(float(header), config.HTTP_BACKOFF_MAX_SECONDS))
    except ValueError:
        return None


class SupabaseDatabase(BaseDatabase):
    """Supabase backend talking to PostgREST with httpx (no extra SDK needed)."""

    backend_name = "supabase"

    def __init__(self, url: str, key: str) -> None:
        self._base_url = url.rstrip("/") + "/rest/v1"
        self._key = key
        self._client: httpx.AsyncClient | None = None

    async def init(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(config.HTTP_TIMEOUT_SECONDS),
            headers={
                "apikey": self._key,
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
            },
        )
        for table in (LEADS_TABLE, SUBSCRIBERS_TABLE):
            try:
                await self._request("GET", table, params={"select": "*", "limit": "1"})
            except DatabaseError as exc:
                await self.close()
                if exc.status_code == 404:
                    raise DatabaseError(
                        f"Supabase table '{table}' was not found. Run the SQL printed by "
                        "`python -m src.database` in the Supabase SQL Editor first.",
                        status_code=404,
                    ) from exc
                raise
        logger.info("Supabase database ready")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        table: str,
        *,
        params: dict[str, str] | None = None,
        json_body: Any = None,
        prefer: str | None = None,
    ) -> httpx.Response:
        if self._client is None:
            raise DatabaseError("SupabaseDatabase.init() must be called before use.")
        url = f"{self._base_url}/{table}"
        headers = {"Prefer": prefer} if prefer else None
        last_error = "unknown error"

        for attempt in range(config.HTTP_MAX_RETRIES):
            retry_after: float | None = None
            try:
                response = await self._client.request(
                    method, url, params=params, json=json_body, headers=headers
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("Supabase %s %s failed (attempt %d): %s", method, table, attempt + 1, last_error)
            else:
                if response.status_code in config.RETRYABLE_STATUS_CODES:
                    last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                    retry_after = _retry_after_seconds(response)
                    logger.warning(
                        "Supabase %s %s retryable status (attempt %d): %s",
                        method, table, attempt + 1, last_error,
                    )
                elif response.status_code >= 400:
                    raise DatabaseError(
                        f"Supabase {method} {table} failed: HTTP {response.status_code} {response.text[:300]}",
                        status_code=response.status_code,
                    )
                else:
                    return response

            if attempt < config.HTTP_MAX_RETRIES - 1:
                await asyncio.sleep(retry_after if retry_after is not None else config.backoff_delay(attempt))

        raise DatabaseError(
            f"Supabase {method} {table} failed after {config.HTTP_MAX_RETRIES} attempts: {last_error}"
        )

    # --- leads -------------------------------------------------------------
    async def filter_new_ids(self, item_ids: Sequence[str]) -> set[str]:
        ids = _unique(item_ids)
        if not ids:
            return set()
        existing: set[str] = set()
        for chunk in _chunks(ids, SUPABASE_ID_CHUNK):
            in_filter = "in.(" + ",".join(_quote_list_value(item_id) for item_id in chunk) + ")"
            response = await self._request(
                "GET", LEADS_TABLE, params={"select": "item_id", "item_id": in_filter}
            )
            existing.update(row["item_id"] for row in response.json())
        return {item_id for item_id in ids if item_id not in existing}

    async def save_lead(self, lead: AnalyzedLead) -> bool:
        row = lead.to_db_row()
        row["created_at"] = (lead.created_at or utc_now()).isoformat()
        response = await self._request(
            "POST",
            LEADS_TABLE,
            params={"on_conflict": "item_id"},
            json_body=row,
            prefer="resolution=ignore-duplicates,return=representation",
        )
        return bool(response.json())

    async def get_pending_vip_leads(self, limit: int = 20) -> list[AnalyzedLead]:
        oldest = (utc_now() - timedelta(hours=config.VIP_RETRY_MAX_AGE_HOURS)).isoformat()
        response = await self._request(
            "GET",
            LEADS_TABLE,
            params={
                "select": "*",
                "quality_score": f"gte.{config.VIP_MIN_SCORE}",
                "sent_to_vip": "eq.0",
                "created_at": f"gte.{oldest}",
                "order": "quality_score.desc,created_at.asc",
                "limit": str(limit),
            },
        )
        return [AnalyzedLead.from_db_row(row) for row in response.json()]

    async def count_vip_sent_since(self, since: datetime) -> int:
        earliest = since - timedelta(hours=config.VIP_RETRY_MAX_AGE_HOURS + 1)
        response = await self._request(
            "GET",
            LEADS_TABLE,
            params={
                "select": "created_at,pitch_template_en",
                "sent_to_vip": "eq.1",
                "created_at": f"gte.{earliest.isoformat()}",
                "limit": "1000",
            },
        )
        return sum(
            1
            for row in response.json()
            if (effective_sent_time(row.get("created_at"), row.get("pitch_template_en") or "") or since) >= since
        )

    async def get_due_free_leads(self, limit: int = 20) -> list[AnalyzedLead]:
        cutoff = (utc_now() - timedelta(hours=config.FREE_DELAY_HOURS)).isoformat()
        oldest = (utc_now() - timedelta(hours=config.FREE_MAX_AGE_HOURS)).isoformat()
        response = await self._request(
            "GET",
            LEADS_TABLE,
            params={
                "select": "*",
                "quality_score": f"gte.{max(config.VIP_MIN_SCORE, config.FREE_MIN_SCORE)}",
                "sent_to_vip": "eq.1",
                "sent_to_free": "eq.0",
                "and": f"(created_at.lte.{cutoff},created_at.gte.{oldest})",
                "order": "created_at.asc",
                "limit": str(limit),
            },
        )
        return [AnalyzedLead.from_db_row(row) for row in response.json()]

    async def mark_sent_vip(self, item_id: str) -> None:
        current = await self._request(
            "GET", LEADS_TABLE, params={"select": "pitch_template_en", "item_id": f"eq.{item_id}", "limit": "1"}
        )
        rows = current.json()
        stored = (rows[0].get("pitch_template_en") if rows else "") or ""
        body: dict[str, Any] = {"sent_to_vip": 1}
        stamped = stamp_sent_at(stored, utc_now())
        if stamped is not None:
            body["pitch_template_en"] = stamped
        await self._request(
            "PATCH",
            LEADS_TABLE,
            params={"item_id": f"eq.{item_id}"},
            json_body=body,
            prefer="return=minimal",
        )

    async def mark_sent_free(self, item_id: str) -> None:
        await self._request(
            "PATCH",
            LEADS_TABLE,
            params={"item_id": f"eq.{item_id}"},
            json_body={"sent_to_free": 1},
            prefer="return=minimal",
        )

    async def cleanup_old_leads(self, days: int) -> int:
        cutoff: datetime = utc_now() - timedelta(days=days)
        response = await self._request(
            "DELETE",
            LEADS_TABLE,
            params={"created_at": f"lt.{cutoff.isoformat()}"},
            prefer="count=exact,return=minimal",
        )
        content_range = response.headers.get("content-range", "")
        total = content_range.rsplit("/", 1)[-1]
        return int(total) if total.isdigit() else 0

    # --- subscribers -------------------------------------------------------
    async def upsert_subscriber(self, subscriber: Subscriber) -> None:
        row = subscriber.to_db_row()
        row["status"] = _validate_status(row["status"])
        row["updated_at"] = utc_now().isoformat()
        await self._request(
            "POST",
            SUBSCRIBERS_TABLE,
            params={"on_conflict": "subscription_id"},
            json_body=row,
            prefer="resolution=merge-duplicates,return=minimal",
        )

    async def _select_subscribers(self, filters: dict[str, str]) -> list[Subscriber]:
        params = {"select": "*", "order": "updated_at.desc", **filters}
        response = await self._request("GET", SUBSCRIBERS_TABLE, params=params)
        return [Subscriber.from_db_row(row) for row in response.json()]

    async def get_subscriber(self, subscription_id: str) -> Subscriber | None:
        found = await self._select_subscribers({"subscription_id": f"eq.{subscription_id}", "limit": "1"})
        return found[0] if found else None

    async def get_subscribers_by_email(self, email: str) -> list[Subscriber]:
        return await self._select_subscribers({"customer_email": f"eq.{email.strip().lower()}"})

    async def get_subscriber_by_invite_link(self, invite_link: str) -> Subscriber | None:
        found = await self._select_subscribers({"invite_link": f"eq.{invite_link}", "limit": "1"})
        return found[0] if found else None

    async def get_subscribers_by_telegram_id(self, telegram_user_id: int) -> list[Subscriber]:
        return await self._select_subscribers({"telegram_user_id": f"eq.{int(telegram_user_id)}"})

    async def _patch_subscriber(self, subscription_id: str, changes: dict[str, Any]) -> bool:
        payload = {**changes, "updated_at": utc_now().isoformat()}
        response = await self._request(
            "PATCH",
            SUBSCRIBERS_TABLE,
            params={"subscription_id": f"eq.{subscription_id}"},
            json_body=payload,
            prefer="return=representation",
        )
        return bool(response.json())

    async def update_subscriber_status(self, subscription_id: str, status: str) -> bool:
        return await self._patch_subscriber(subscription_id, {"status": _validate_status(status)})

    async def set_telegram_user_id(self, subscription_id: str, telegram_user_id: int) -> bool:
        return await self._patch_subscriber(subscription_id, {"telegram_user_id": int(telegram_user_id)})

    # --- support desk ------------------------------------------------------
    async def create_support_ticket(self, telegram_user_id: int, name: str, username: str) -> SupportTicket:
        response = await self._request(
            "POST",
            SUPPORT_TICKETS_TABLE,
            json_body={"telegram_user_id": int(telegram_user_id), "name": name, "username": username},
            prefer="return=representation",
        )
        return SupportTicket.from_db_row(response.json()[0])

    async def _select_tickets(self, filters: dict[str, str]) -> list[SupportTicket]:
        response = await self._request(
            "GET", SUPPORT_TICKETS_TABLE, params={"select": "*", "order": "updated_at.desc", **filters}
        )
        return [SupportTicket.from_db_row(row) for row in response.json()]

    async def get_support_ticket(self, ticket_id: int) -> SupportTicket | None:
        found = await self._select_tickets({"id": f"eq.{int(ticket_id)}", "limit": "1"})
        return found[0] if found else None

    async def get_open_support_ticket(self, telegram_user_id: int) -> SupportTicket | None:
        found = await self._select_tickets(
            {"telegram_user_id": f"eq.{int(telegram_user_id)}", "status": "eq.open", "limit": "1"}
        )
        return found[0] if found else None

    async def close_support_ticket(self, ticket_id: int) -> bool:
        response = await self._request(
            "PATCH",
            SUPPORT_TICKETS_TABLE,
            params={"id": f"eq.{int(ticket_id)}"},
            json_body={"status": "closed", "updated_at": utc_now().isoformat()},
            prefer="return=representation",
        )
        return bool(response.json())

    async def list_open_support_tickets(self, limit: int = 20) -> list[SupportTicket]:
        return await self._select_tickets({"status": "eq.open", "limit": str(int(limit))})

    async def add_support_message(
        self, ticket_id: int | None, telegram_user_id: int, direction: str, body: str
    ) -> None:
        await self._request(
            "POST",
            SUPPORT_MESSAGES_TABLE,
            json_body={
                "ticket_id": ticket_id,
                "telegram_user_id": int(telegram_user_id),
                "direction": direction,
                "body": body,
            },
            prefer="return=minimal",
        )
        if ticket_id is not None:
            await self._request(
                "PATCH",
                SUPPORT_TICKETS_TABLE,
                params={"id": f"eq.{int(ticket_id)}"},
                json_body={"updated_at": utc_now().isoformat()},
                prefer="return=minimal",
            )

    async def get_support_messages(self, telegram_user_id: int, limit: int = 15) -> list[SupportMessage]:
        response = await self._request(
            "GET",
            SUPPORT_MESSAGES_TABLE,
            params={
                "select": "*",
                "telegram_user_id": f"eq.{int(telegram_user_id)}",
                "order": "created_at.desc",
                "limit": str(int(limit)),
            },
        )
        return [SupportMessage.from_db_row(row) for row in reversed(response.json())]

    async def is_support_blocked(self, telegram_user_id: int) -> bool:
        response = await self._request(
            "GET",
            SUPPORT_BLOCKED_TABLE,
            params={"select": "telegram_user_id", "telegram_user_id": f"eq.{int(telegram_user_id)}", "limit": "1"},
        )
        return bool(response.json())

    async def set_support_blocked(self, telegram_user_id: int, blocked: bool) -> None:
        if blocked:
            await self._request(
                "POST",
                SUPPORT_BLOCKED_TABLE,
                params={"on_conflict": "telegram_user_id"},
                json_body={"telegram_user_id": int(telegram_user_id)},
                prefer="resolution=merge-duplicates,return=minimal",
            )
        else:
            await self._request(
                "DELETE",
                SUPPORT_BLOCKED_TABLE,
                params={"telegram_user_id": f"eq.{int(telegram_user_id)}"},
                prefer="return=minimal",
            )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def create_database() -> BaseDatabase:
    """Supabase when SUPABASE_URL and SUPABASE_KEY are set, otherwise local SQLite."""
    if config.SUPABASE_URL and config.SUPABASE_KEY:
        return SupabaseDatabase(config.SUPABASE_URL, config.SUPABASE_KEY)
    return SQLiteDatabase(config.SQLITE_PATH)


if __name__ == "__main__":
    print(SUPABASE_SCHEMA_SQL)
