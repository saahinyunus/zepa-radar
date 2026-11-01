"""Support desk bot (customers write, the operator answers from Telegram).

Flow
----
1. A customer messages the support bot (text, or a photo/file with a caption).
2. The message is copied to the operator's private chat, headed by
   `Ticket ZR-XXXXX | user <telegram id>` plus the sender's name and plan.
3. The operator answers by *replying* to that message; the reply goes back to the customer.

No state is stored: the ticket header carries everything needed to route the answer. A ticket
reference is stable per customer and UTC day, so one conversation keeps one reference.

The operator must open the support bot once and press Start, otherwise Telegram refuses to
let a bot message them.
"""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict, deque
from typing import Any

from src import config
from src.database import BaseDatabase
from src.models import SUBSCRIBER_STATUS_ACTIVE
from src.telegram_notifier import TelegramNotifier, _clip

logger = logging.getLogger("support")

TICKET_PATTERN = re.compile(r"Ticket (ZR-[0-9A-Z]+) \| user (\d+)")
# Media Telegram lets us re-caption while copying it to the operator.
MEDIA_KEYS = ("photo", "document", "video", "audio", "voice", "animation")
BASE36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
RATE_WINDOW_SECONDS = 3600
ACK_EVERY_SECONDS = 600
MAX_TEXT_CHARS = 3500
MAX_CAPTION_CHARS = 1000

NO_TICKET_HINT = (
    "To answer a customer, reply to their ticket message (swipe it / use Reply). "
    "Your reply is delivered to them."
)
UNSUPPORTED_TEXT = "Sorry, I can only read text, photos and files. Please describe the problem in a message."
SLOW_DOWN_TEXT = "You are sending a lot of messages. Please wait a little; we have your earlier ones."
UNAVAILABLE_TEXT = "Support is temporarily unavailable. Please try again in a little while."


def ticket_ref(user_id: int, now: float | None = None) -> str:
    """Short reference, the same for one customer during one UTC day."""
    day = int((time.time() if now is None else now) // 86400)
    number = (user_id * 7919 + day) % 36**5
    digits = ""
    for _ in range(5):
        number, rest = divmod(number, 36)
        digits = BASE36[rest] + digits
    return f"ZR-{digits}"


def welcome_text() -> str:
    main_bot = f"@{config.TELEGRAM_BOT_USERNAME}" if config.TELEGRAM_BOT_USERNAME else "the Zepa Radar bot"
    return (
        "👋 Hi, this is Zepa Radar Support.\n\n"
        "Write your problem, question or suggestion here and we will open a ticket. "
        "Screenshots and files are welcome. We answer in this chat, usually within 24 hours.\n\n"
        "💳 Billing: to cancel or update your card, use the link in your Lemon Squeezy receipt "
        "e-mail (or app.lemonsqueezy.com/my-orders).\n"
        f"🔑 VIP access: send your checkout e-mail to {main_bot} to get your invite link."
    )


def ticket_header(ref: str, sender: dict[str, Any], plan: str) -> str:
    name = " ".join(part for part in (sender.get("first_name"), sender.get("last_name")) if part) or "unknown"
    handle = f" (@{sender['username']})" if sender.get("username") else ""
    return f"🎫 Ticket {ref} | user {sender['id']}\n{name}{handle} · {plan}"


class SupportService:
    """Routes customer messages to the operator and the operator's replies back."""

    def __init__(self, db: BaseDatabase, notifier: TelegramNotifier, alert_notifier: TelegramNotifier | None = None) -> None:
        self.db = db
        self.notifier = notifier  # the support bot
        self.alert_notifier = alert_notifier  # the main bot, used to warn the operator
        self._recent: dict[int, deque[float]] = defaultdict(deque)
        self._last_ack: dict[int, float] = {}

    # --- entry point -------------------------------------------------------
    async def handle_update(self, update: dict[str, Any]) -> str:
        message = update.get("message")
        if not isinstance(message, dict):
            return "ignored"
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id, user_id = chat.get("id"), sender.get("id")
        if chat.get("type") != "private" or not isinstance(chat_id, int) or not isinstance(user_id, int):
            return "ignored"
        if sender.get("is_bot"):
            return "ignored"
        admin = str(config.TELEGRAM_ADMIN_CHAT_ID)
        if admin and str(chat_id) == admin:
            return await self._handle_operator(message, chat_id)
        return await self._handle_customer(message, chat_id, sender)

    # --- helpers -----------------------------------------------------------
    async def _say(self, chat_id: int | str, text: str) -> bool:
        result = await self.notifier.call_api(
            "sendMessage", {"chat_id": chat_id, "text": text[:4096], "disable_web_page_preview": True}
        )
        return result is not None

    @staticmethod
    def _command(text: str) -> str:
        if not text.startswith("/"):
            return ""
        return text.split()[0].split("@")[0].lower()

    def _allow(self, user_id: int, now: float) -> bool:
        window = self._recent[user_id]
        while window and now - window[0] > RATE_WINDOW_SECONDS:
            window.popleft()
        if len(window) >= config.SUPPORT_MAX_MESSAGES_PER_HOUR:
            return False
        window.append(now)
        if len(self._recent) > 5000:  # forget idle users so the dict cannot grow without bound
            for key in [k for k, v in self._recent.items() if not v]:
                del self._recent[key]
        return True

    async def _plan(self, user_id: int) -> str:
        try:
            subscribers = await self.db.get_subscribers_by_telegram_id(user_id)
        except Exception:  # noqa: BLE001 - a lookup problem must not block support
            logger.warning("Subscriber lookup failed for %s", user_id, exc_info=True)
            return "plan unknown"
        if not subscribers:
            return "no linked subscription"
        if any(s.status == SUBSCRIBER_STATUS_ACTIVE for s in subscribers):
            return "VIP active"
        return f"VIP {subscribers[0].status}"

    async def _warn_operator(self, text: str) -> None:
        if self.alert_notifier is None:
            return
        try:
            await self.alert_notifier.send_admin_alert(text, title="🛟 Zepa Radar Support")
        except Exception:  # noqa: BLE001
            logger.warning("Support alert failed", exc_info=True)

    # --- customer -> operator ----------------------------------------------
    async def _handle_customer(self, message: dict[str, Any], chat_id: int, sender: dict[str, Any]) -> str:
        user_id = sender["id"]
        text = str(message.get("text") or "").strip()
        command = self._command(text)
        if command in ("/start", "/help"):
            await self._say(chat_id, welcome_text())
            return "welcome"

        has_media = any(key in message for key in MEDIA_KEYS)
        if not text and not has_media:
            await self._say(chat_id, UNSUPPORTED_TEXT)
            return "unsupported"
        if not config.TELEGRAM_ADMIN_CHAT_ID:
            await self._say(chat_id, UNAVAILABLE_TEXT)
            return "no_operator"

        now = time.time()
        if not self._allow(user_id, now):
            await self._say(chat_id, SLOW_DOWN_TEXT)
            return "rate_limited"

        ref = ticket_ref(user_id, now)
        header = ticket_header(ref, sender, await self._plan(user_id))
        if has_media:
            caption = _clip(str(message.get("caption") or ""), max(0, MAX_CAPTION_CHARS - len(header) - 2))
            delivered = await self.notifier.call_api(
                "copyMessage",
                {
                    "chat_id": config.TELEGRAM_ADMIN_CHAT_ID,
                    "from_chat_id": chat_id,
                    "message_id": message.get("message_id"),
                    "caption": f"{header}\n\n{caption}".strip(),
                },
            )
        else:
            delivered = await self.notifier.call_api(
                "sendMessage",
                {"chat_id": config.TELEGRAM_ADMIN_CHAT_ID, "text": f"{header}\n\n{_clip(text, MAX_TEXT_CHARS)}"},
            )

        if delivered is None:
            logger.error("Support message from %s could not be delivered to the operator", user_id)
            await self._warn_operator(
                "A customer wrote to the support bot but I could not deliver it to you. "
                "Open the support bot and press Start, then ask the customer to write again."
            )
            await self._say(chat_id, UNAVAILABLE_TEXT)
            return "delivery_failed"

        if now - self._last_ack.get(user_id, 0.0) > ACK_EVERY_SECONDS:
            self._last_ack[user_id] = now
            await self._say(chat_id, f"✅ Got it. Your ticket is {ref}. We will reply here, usually within 24 hours.")
        return "forwarded"

    # --- operator -> customer ----------------------------------------------
    async def _handle_operator(self, message: dict[str, Any], chat_id: int) -> str:
        text = str(message.get("text") or "").strip()
        if self._command(text) in ("/start", "/help"):
            await self._say(chat_id, f"✅ Support inbox is ready. {NO_TICKET_HINT}")
            return "operator_help"

        replied = message.get("reply_to_message") or {}
        match = TICKET_PATTERN.search(str(replied.get("text") or replied.get("caption") or ""))
        if not match:
            await self._say(chat_id, NO_TICKET_HINT)
            return "operator_no_ticket"
        ref, customer_id = match.group(1), int(match.group(2))

        if text:
            delivered = await self.notifier.call_api(
                "sendMessage",
                {"chat_id": customer_id, "text": f"💬 Zepa Radar Support ({ref})\n\n{text}"[:4096]},
            )
        else:  # photo, file, voice...: pass it through unchanged
            delivered = await self.notifier.call_api(
                "copyMessage",
                {"chat_id": customer_id, "from_chat_id": chat_id, "message_id": message.get("message_id")},
            )
        if delivered is None:
            await self._say(chat_id, f"❌ {ref}: not delivered (the customer may have blocked the bot).")
            return "operator_delivery_failed"
        await self._say(chat_id, f"✅ {ref}: delivered.")
        return "operator_replied"
