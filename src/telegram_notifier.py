"""Telegram delivery: VIP supergroup topics (instant) and Free channel (delayed teaser).

    async with TelegramNotifier() as notifier:
        await notifier.send_vip_leads(leads, db)

`call_api` is public so the Lemon Squeezy webhook can reuse the same retrying
client for `createChatInviteLink`, `banChatMember`, etc.
"""
from __future__ import annotations

import asyncio
import html
import logging
from typing import Any, Sequence

import httpx

from src import config
from src.database import BaseDatabase
from src.models import AnalyzedLead

logger = logging.getLogger(__name__)

VIP_BUTTON_TEXT = "🔗 Open Post & Apply Now (Direct Link)"
FREE_BUTTON_TEXT = "🚀 Get Instant Client Alerts ($19/mo)"


def _esc(value: Any) -> str:
    """Escape text for Telegram's HTML parse mode."""
    return html.escape(str(value or "").strip(), quote=False)


def _clip(value: str, limit: int) -> str:
    value = (value or "").strip()
    return value if len(value) <= limit else value[: max(0, limit - 1)].rstrip() + "…"


def _is_http_url(value: str) -> bool:
    return value.lower().startswith(("http://", "https://"))


def _hours_label(hours: float) -> str:
    return f"{hours:g}"


def format_vip_message(lead: AnalyzedLead) -> str:
    """HTML message for the VIP topic. Shrinks long fields until it fits Telegram's limit."""
    category = config.CATEGORY_DISPLAY_NAMES.get(lead.category, lead.category)
    limits = (700, 300)
    message = ""
    for scale in (1.0, 0.7, 0.45, 0.25):
        summary_max, pain_max = (int(limit * scale) for limit in limits)
        message = (
            f"🔥 <b>NEW {_esc(category)} OPPORTUNITY</b> (Score: {lead.quality_score}/100)\n\n"
            f"💰 <b>Budget:</b> {_esc(lead.estimated_budget or 'Not specified')} | "
            f"🌐 <b>Source:</b> {_esc(lead.source)}\n\n"
            f"📋 <b>What They Need:</b>\n{_esc(_clip(lead.summary_en, summary_max))}\n\n"
            f"🎯 <b>Key Pain Point:</b> {_esc(_clip(lead.client_pain_point, pain_max))}"
        )
        if len(message) <= config.TELEGRAM_MAX_MESSAGE_CHARS:
            break
    return message


def format_free_message(lead: AnalyzedLead) -> str:
    """HTML teaser for the Free channel (no link to the client's post)."""
    category = config.CATEGORY_DISPLAY_NAMES.get(lead.category, lead.category)
    hours = _hours_label(config.FREE_DELAY_HOURS)
    return (
        f"⏳ <b>[{hours} HOURS AGO] {_esc(category)} Lead Captured by VIP Radar</b>\n\n"
        f"{_esc(_clip(lead.summary_en, 700))}\n"
        f"💰 Budget: {_esc(lead.estimated_budget or 'Not specified')}\n\n"
        f"🔒 <i>The direct client link was delivered to VIP members "
        f"{hours} hours ago. Don't miss the next client!</i>"
    )


def vip_keyboard(lead: AnalyzedLead) -> dict[str, Any] | None:
    if not _is_http_url(lead.url):
        return None
    return {"inline_keyboard": [[{"text": VIP_BUTTON_TEXT, "url": lead.url}]]}


def free_keyboard() -> dict[str, Any] | None:
    if not _is_http_url(config.LEMON_SQUEEZY_CHECKOUT_URL):
        return None
    return {"inline_keyboard": [[{"text": FREE_BUTTON_TEXT, "url": config.LEMON_SQUEEZY_CHECKOUT_URL}]]}


class TelegramNotifier:
    """Async Telegram Bot API client with retry and flood-control handling."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        token: str | None = None,
        vip_group_id: str | None = None,
        free_channel_id: str | None = None,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._token = config.TELEGRAM_BOT_TOKEN if token is None else token
        self.vip_group_id = config.TELEGRAM_VIP_GROUP_ID if vip_group_id is None else vip_group_id
        self.free_channel_id = (
            config.TELEGRAM_FREE_CHANNEL_ID if free_channel_id is None else free_channel_id
        )

    async def __aenter__(self) -> "TelegramNotifier":
        self._ensure_client()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(config.HTTP_TIMEOUT_SECONDS))
            self._owns_client = True
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    # --- low level ---------------------------------------------------------
    async def call_api(self, method: str, payload: dict[str, Any]) -> Any | None:
        """POST a Bot API method. Returns the `result` field, or None on failure.

        Retries timeouts, 5xx and flood-control 429 (honouring `retry_after`).
        Permanent errors (400/403/...) are logged once and not retried. The bot
        token is never written to logs.
        """
        if not self._token:
            logger.error("TELEGRAM_BOT_TOKEN is not set - cannot call %s", method)
            return None

        client = self._ensure_client()
        url = f"{config.TELEGRAM_API_BASE}/bot{self._token}/{method}"
        last_error = "unknown error"

        for attempt in range(config.HTTP_MAX_RETRIES):
            wait = config.backoff_delay(attempt)
            try:
                response = await client.post(url, json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = type(exc).__name__
                logger.warning("Telegram %s request error (attempt %d): %s", method, attempt + 1, last_error)
            else:
                body = _safe_json(response)
                if response.status_code == 200 and body.get("ok"):
                    return body.get("result", True)

                description = str(body.get("description") or response.text[:200])
                last_error = f"HTTP {response.status_code}: {description}"
                if response.status_code == 429:
                    retry_after = (body.get("parameters") or {}).get("retry_after")
                    if isinstance(retry_after, (int, float)):
                        if retry_after > config.HTTP_BACKOFF_MAX_SECONDS:
                            logger.error("Telegram flood control (%ss) on %s - giving up", retry_after, method)
                            return None
                        wait = float(retry_after) + 0.5
                    logger.warning("Telegram %s rate limited, waiting %.1fs", method, wait)
                elif response.status_code < 500:
                    logger.error("Telegram %s failed: %s", method, last_error)
                    return None
                else:
                    logger.warning("Telegram %s server error (attempt %d): %s", method, attempt + 1, last_error)

            if attempt < config.HTTP_MAX_RETRIES - 1:
                await asyncio.sleep(wait)

        logger.error("Telegram %s failed after %d attempts: %s", method, config.HTTP_MAX_RETRIES, last_error)
        return None

    async def _send_message(
        self,
        chat_id: str,
        text: str,
        *,
        thread_id: int | None = None,
        keyboard: dict[str, Any] | None = None,
    ) -> bool:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if thread_id is not None:
            payload["message_thread_id"] = thread_id
        if keyboard is not None:
            payload["reply_markup"] = keyboard
        return await self.call_api("sendMessage", payload) is not None

    # --- lead delivery -----------------------------------------------------
    async def send_vip_lead(self, lead: AnalyzedLead) -> bool:
        """Send one lead to the VIP supergroup, inside its category topic."""
        if not self.vip_group_id:
            logger.error("TELEGRAM_VIP_GROUP_ID is not set")
            return False
        thread_id = config.get_topic_id(lead.category)
        if thread_id is None:
            logger.warning("No topic ID for %s - sending to the General topic", lead.category)
        return await self._send_message(
            self.vip_group_id,
            format_vip_message(lead),
            thread_id=thread_id,
            keyboard=vip_keyboard(lead),
        )

    async def send_free_lead(self, lead: AnalyzedLead) -> bool:
        """Send the delayed, link-less teaser to the Free channel."""
        if not self.free_channel_id:
            logger.error("TELEGRAM_FREE_CHANNEL_ID is not set")
            return False
        return await self._send_message(
            self.free_channel_id,
            format_free_message(lead),
            keyboard=free_keyboard(),
        )

    async def send_admin_alert(self, text: str) -> bool:
        """Private health message to the operator. No-op when TELEGRAM_ADMIN_CHAT_ID is unset."""
        if not config.TELEGRAM_ADMIN_CHAT_ID:
            return False
        result = await self.call_api(
            "sendMessage",
            {"chat_id": config.TELEGRAM_ADMIN_CHAT_ID, "text": f"⚠️ Radar alert\n{text}"[:4000]},
        )
        return result is not None

    async def send_vip_leads(self, leads: Sequence[AnalyzedLead], db: BaseDatabase) -> int:
        """Send leads to VIP one by one; each success is flagged `sent_to_vip` immediately.

        Failed leads stay unflagged and are retried next run while still fresh.
        """
        sent = 0
        for index, lead in enumerate(leads):
            if index > 0:
                await asyncio.sleep(config.TELEGRAM_SEND_INTERVAL_SECONDS)
            if await self.send_vip_lead(lead):
                await db.mark_sent_vip(lead.item_id)
                sent += 1
        logger.info("VIP: %d/%d lead(s) delivered", sent, len(leads))
        return sent

    async def send_free_leads(self, leads: Sequence[AnalyzedLead], db: BaseDatabase) -> int:
        """Send due teasers to the Free channel, flagging each as `sent_to_free`."""
        sent = 0
        for index, lead in enumerate(leads):
            if index > 0:
                await asyncio.sleep(config.TELEGRAM_SEND_INTERVAL_SECONDS)
            if await self.send_free_lead(lead):
                await db.mark_sent_free(lead.item_id)
                sent += 1
        logger.info("Free: %d/%d teaser(s) delivered", sent, len(leads))
        return sent


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
