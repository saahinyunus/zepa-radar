"""Lemon Squeezy + Telegram webhook service (FastAPI).

Flow
----
1. Lemon Squeezy calls `POST /webhooks/lemonsqueezy` (HMAC-SHA256 `X-Signature`).
   * subscription_created / subscription_payment_success / resumed / unpaused
       -> subscriber becomes `active`, a single-use VIP invite link is created
          (`createChatInviteLink`, `member_limit=1`) and stored.
   * subscription_expired -> subscriber `expired`, removed from the VIP group.
   * subscription_cancelled -> subscriber `cancelled`; removed immediately only if the
       paid period has already ended (otherwise at the later `subscription_expired`).
2. The customer DMs the bot with the e-mail used at checkout (Telegram webhook
   `POST /webhooks/telegram`) and receives a fresh single-use invite link. The
   Telegram account is bound to the subscription at that moment.
3. When the user joins through the link, Telegram sends a `chat_member` update; the
   service verifies the joiner against the bound account and removes intruders.

Run:   python -m src.lemon_webhook serve
Hook:  python -m src.lemon_webhook set-webhook https://your-public-host

The bot must be an admin of the VIP group (invite users + ban users).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from src import config
from src.database import BaseDatabase, create_database
from src.models import (
    SUBSCRIBER_STATUS_ACTIVE,
    SUBSCRIBER_STATUS_CANCELLED,
    SUBSCRIBER_STATUS_EXPIRED,
    Subscriber,
    parse_timestamp,
    utc_now,
)
from src.telegram_notifier import TelegramNotifier

logger = logging.getLogger("webhook")

ACTIVATE_EVENTS = frozenset(
    {
        "subscription_created",
        "subscription_payment_success",
        "subscription_resumed",
        "subscription_unpaused",
    }
)
INACTIVE_LEMON_STATUSES = frozenset({"expired", "cancelled", "unpaid", "paused"})
JOINED_STATUSES = frozenset({"member", "administrator", "creator", "restricted"})
LEFT_STATUSES = frozenset({"left", "kicked"})
EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

START_TEXT = (
    "👋 Welcome to the Freelance Radar VIP bot.\n\n"
    "Send me the <b>e-mail address you used at checkout</b> and I will reply with your "
    "personal one-time VIP group invite link."
)


class ProcessingError(Exception):
    """Transient failure; the route answers 500 so Lemon Squeezy retries the delivery."""


@dataclass(slots=True)
class SubscriptionEvent:
    event_name: str
    subscription_id: str
    email: str
    lemon_status: str
    ends_at: datetime | None
    test_mode: bool = False


def verify_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    """Constant-time check of Lemon Squeezy's hex HMAC-SHA256 `X-Signature`."""
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


def parse_event(payload: Any) -> SubscriptionEvent | None:
    """Extract the fields we need; None when the payload is not a subscription event."""
    if not isinstance(payload, dict):
        return None
    meta = payload.get("meta") or {}
    data = payload.get("data") or {}
    attributes = data.get("attributes") or {}
    event_name = str(meta.get("event_name") or "")
    if not event_name.startswith("subscription_"):
        return None

    # Invoice events carry the subscription id as an attribute, others as data.id.
    if str(data.get("type")) == "subscription-invoices":
        subscription_id = attributes.get("subscription_id")
    else:
        subscription_id = data.get("id")
    if subscription_id in (None, ""):
        return None

    return SubscriptionEvent(
        event_name=event_name,
        subscription_id=str(subscription_id),
        email=str(attributes.get("user_email") or "").strip().lower(),
        lemon_status=str(attributes.get("status") or "").lower(),
        ends_at=parse_timestamp(attributes.get("ends_at")),
        test_mode=bool(meta.get("test_mode")),
    )


class SubscriptionService:
    """Business logic, independent of FastAPI so it can be tested directly."""

    def __init__(self, db: BaseDatabase, notifier: TelegramNotifier) -> None:
        self.db = db
        self.notifier = notifier
        self._lock = asyncio.Lock()

    # --- Telegram helpers --------------------------------------------------
    async def create_invite_link(self, subscription_id: str) -> str | None:
        payload = {
            "chat_id": self.notifier.vip_group_id,
            "member_limit": 1,
            "expire_date": int(time.time()) + config.INVITE_LINK_TTL_HOURS * 3600,
            "name": f"sub {subscription_id}"[:32],
        }
        result = await self.notifier.call_api("createChatInviteLink", payload)
        link = result.get("invite_link") if isinstance(result, dict) else None
        return str(link) if link else None

    async def revoke_invite_link(self, invite_link: str | None) -> None:
        if invite_link:
            await self.notifier.call_api(
                "revokeChatInviteLink",
                {"chat_id": self.notifier.vip_group_id, "invite_link": invite_link},
            )

    async def kick_user(self, telegram_user_id: int) -> bool:
        """Remove a user from the VIP group without a permanent ban (they may re-subscribe)."""
        banned = await self.notifier.call_api(
            "banChatMember",
            {"chat_id": self.notifier.vip_group_id, "user_id": int(telegram_user_id)},
        )
        if banned is None:
            return False
        await self.notifier.call_api(
            "unbanChatMember",
            {
                "chat_id": self.notifier.vip_group_id,
                "user_id": int(telegram_user_id),
                "only_if_banned": True,
            },
        )
        return True

    async def remove_member(self, subscriber: Subscriber) -> None:
        await self.revoke_invite_link(subscriber.invite_link)
        if subscriber.telegram_user_id is None:
            logger.info("Subscription %s has no linked Telegram user - nothing to kick", subscriber.subscription_id)
            return
        if await self.kick_user(subscriber.telegram_user_id):
            logger.info("Removed user %s (subscription %s)", subscriber.telegram_user_id, subscriber.subscription_id)
        else:
            raise ProcessingError(f"Could not remove user {subscriber.telegram_user_id} from the VIP group")

    # --- Lemon Squeezy events ----------------------------------------------
    async def handle_event(self, event: SubscriptionEvent) -> str:
        if event.test_mode and not config.LEMON_SQUEEZY_ALLOW_TEST_MODE:
            logger.warning("Ignoring test-mode event %s for %s", event.event_name, event.subscription_id)
            return "ignored_test_mode"
        async with self._lock:
            if event.event_name in ACTIVATE_EVENTS:
                if event.event_name != "subscription_payment_success" and (
                    event.lemon_status in INACTIVE_LEMON_STATUSES
                ):
                    return "ignored"
                await self._activate(event)
                return "activated"
            if event.event_name == "subscription_expired":
                await self._deactivate(event, SUBSCRIBER_STATUS_EXPIRED)
                return "expired"
            if event.event_name == "subscription_cancelled":
                if event.ends_at is None or event.ends_at <= utc_now():
                    await self._deactivate(event, SUBSCRIBER_STATUS_CANCELLED)
                    return "cancelled_removed"
                # Paid period still running: keep access, Lemon Squeezy sends
                # subscription_expired when it ends.
                await self._set_status(event, SUBSCRIBER_STATUS_CANCELLED)
                return "cancelled_until_period_end"
        return "ignored"

    async def _activate(self, event: SubscriptionEvent) -> None:
        existing = await self.db.get_subscriber(event.subscription_id)
        email = event.email or (existing.customer_email if existing else "")
        if not email:
            raise ProcessingError(f"No e-mail for subscription {event.subscription_id}")

        needs_link = (
            existing is None
            or existing.status != SUBSCRIBER_STATUS_ACTIVE
            or (not existing.invite_link and existing.telegram_user_id is None)
        )
        await self.db.upsert_subscriber(
            Subscriber(event.subscription_id, email, SUBSCRIBER_STATUS_ACTIVE)
        )
        if not needs_link:
            return

        link = await self.create_invite_link(event.subscription_id)
        if link is None:
            raise ProcessingError(f"Invite link creation failed for {event.subscription_id}")
        await self.db.upsert_subscriber(
            Subscriber(event.subscription_id, email, SUBSCRIBER_STATUS_ACTIVE, invite_link=link)
        )
        logger.info("Subscription %s activated, invite link stored", event.subscription_id)

    async def _set_status(self, event: SubscriptionEvent, status: str) -> Subscriber | None:
        existing = await self.db.get_subscriber(event.subscription_id)
        if existing is None:
            if event.email:
                await self.db.upsert_subscriber(Subscriber(event.subscription_id, event.email, status))
            return None
        await self.db.update_subscriber_status(event.subscription_id, status)
        return existing

    async def _deactivate(self, event: SubscriptionEvent, status: str) -> None:
        subscriber = await self._set_status(event, status)
        if subscriber is not None:
            await self.remove_member(subscriber)

    # --- Telegram updates ---------------------------------------------------
    async def handle_telegram_update(self, update: dict[str, Any]) -> str:
        chat_member = update.get("chat_member")
        if isinstance(chat_member, dict):
            return await self._handle_chat_member(chat_member)
        message = update.get("message")
        if isinstance(message, dict):
            return await self._handle_private_message(message)
        return "ignored"

    async def _handle_chat_member(self, chat_member: dict[str, Any]) -> str:
        chat = chat_member.get("chat") or {}
        if str(chat.get("id")) != str(self.notifier.vip_group_id):
            return "ignored"
        old_status = (chat_member.get("old_chat_member") or {}).get("status")
        new_member = chat_member.get("new_chat_member") or {}
        if old_status not in LEFT_STATUSES or new_member.get("status") not in JOINED_STATUSES:
            return "ignored"

        user_id = (new_member.get("user") or {}).get("id")
        link = (chat_member.get("invite_link") or {}).get("invite_link")
        if not isinstance(user_id, int) or not link:
            return "ignored"

        async with self._lock:
            subscriber = await self.db.get_subscriber_by_invite_link(link)
            if subscriber is None:
                return "ignored"
            if subscriber.status != SUBSCRIBER_STATUS_ACTIVE:
                await self.kick_user(user_id)
                return "kicked_inactive"
            if subscriber.telegram_user_id not in (None, user_id):
                logger.warning("Invite for %s used by a different account - removing", subscriber.subscription_id)
                await self.kick_user(user_id)
                return "kicked_wrong_account"
            if subscriber.telegram_user_id is None:
                await self.db.set_telegram_user_id(subscriber.subscription_id, user_id)
            return "linked"

    async def _handle_private_message(self, message: dict[str, Any]) -> str:
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id, user_id = chat.get("id"), sender.get("id")
        if chat.get("type") != "private" or not isinstance(chat_id, int) or not isinstance(user_id, int):
            return "ignored"

        text = str(message.get("text") or "").strip()
        match = EMAIL_PATTERN.search(text)
        reply = START_TEXT if not match else await self.claim_by_email(user_id, match.group(0))
        await self.notifier.call_api(
            "sendMessage",
            {"chat_id": chat_id, "text": reply, "parse_mode": "HTML", "disable_web_page_preview": True},
        )
        return "replied"

    async def claim_by_email(self, telegram_user_id: int, email: str) -> str:
        """Issue (and bind) a fresh single-use invite link for an active subscription."""
        async with self._lock:
            subscribers = await self.db.get_subscribers_by_email(email)
            active = [s for s in subscribers if s.status == SUBSCRIBER_STATUS_ACTIVE]
            if not active:
                checkout = config.LEMON_SQUEEZY_CHECKOUT_URL
                suffix = f"\n\nSubscribe here: {html.escape(checkout)}" if checkout else ""
                return "❌ I couldn't find an active subscription for that e-mail." + suffix

            subscriber = active[0]
            if subscriber.telegram_user_id not in (None, telegram_user_id):
                return (
                    "⚠️ This subscription is already linked to another Telegram account. "
                    "Please contact support if this is a mistake."
                )

            await self.revoke_invite_link(subscriber.invite_link)
            link = await self.create_invite_link(subscriber.subscription_id)
            if link is None:
                return "⚠️ I couldn't create your invite link right now. Please try again in a minute."

            await self.db.upsert_subscriber(
                Subscriber(
                    subscriber.subscription_id,
                    subscriber.customer_email,
                    SUBSCRIBER_STATUS_ACTIVE,
                    telegram_user_id=telegram_user_id,
                    invite_link=link,
                )
            )
            return (
                "✅ Subscription found! Your personal VIP invite link (single use, valid "
                f"{config.INVITE_LINK_TTL_HOURS} hours):\n{html.escape(link)}"
            )


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config.configure_logging()
    db = create_database()
    await db.init()
    notifier = TelegramNotifier()
    app.state.service = SubscriptionService(db, notifier)
    try:
        yield
    finally:
        await notifier.close()
        await db.close()


app = FastAPI(title="Freelance Radar Webhooks", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/welcome", response_class=HTMLResponse)
async def welcome() -> str:
    username = html.escape(config.TELEGRAM_BOT_USERNAME)
    button = (
        f'<p><a href="https://t.me/{username}" style="font-size:1.2em">Open @{username} on Telegram</a></p>'
        if username
        else ""
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' "
        "content='width=device-width,initial-scale=1'><title>Welcome</title></head>"
        "<body style='font-family:sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem'>"
        "<h1>🎉 Thank you!</h1>"
        "<p>Open our Telegram bot and send it the e-mail address you used at checkout. "
        "It will reply with your personal VIP group invite link.</p>"
        f"{button}</body></html>"
    )


@app.post("/webhooks/lemonsqueezy")
async def lemon_squeezy_webhook(
    request: Request,
    x_signature: str | None = Header(default=None),
) -> JSONResponse:
    if not config.LEMON_SQUEEZY_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Webhook secret is not configured")

    raw_body = await request.body()
    if not verify_signature(raw_body, x_signature, config.LEMON_SQUEEZY_WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        event = parse_event(json.loads(raw_body))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON") from None
    if event is None:
        return JSONResponse({"ok": True, "result": "ignored"})

    service: SubscriptionService = request.app.state.service
    try:
        result = await service.handle_event(event)
    except ProcessingError as exc:
        logger.error("Event %s for %s failed: %s", event.event_name, event.subscription_id, exc)
        raise HTTPException(status_code=500, detail="Processing failed, please retry") from exc
    except Exception as exc:  # noqa: BLE001 - any failure must trigger a Lemon Squeezy retry
        logger.exception("Unexpected error handling %s", event.event_name)
        raise HTTPException(status_code=500, detail="Processing failed, please retry") from exc

    logger.info("Event %s (%s) -> %s", event.event_name, event.subscription_id, result)
    return JSONResponse({"ok": True, "result": result})


@app.post("/webhooks/telegram")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> JSONResponse:
    secret = config.TELEGRAM_WEBHOOK_SECRET
    if not secret:
        raise HTTPException(status_code=503, detail="Telegram webhook secret is not configured")
    if not hmac.compare_digest(secret, x_telegram_bot_api_secret_token or ""):
        raise HTTPException(status_code=401, detail="Invalid secret token")

    try:
        update = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON") from None
    if not isinstance(update, dict):
        return JSONResponse({"ok": True})

    service: SubscriptionService = request.app.state.service
    try:
        result = await service.handle_telegram_update(update)
    except Exception:  # noqa: BLE001 - never make Telegram retry-loop on a bad update
        logger.exception("Telegram update handling failed")
        return JSONResponse({"ok": False})
    return JSONResponse({"ok": True, "result": result})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
async def _set_telegram_webhook(public_url: str) -> int:
    if not config.TELEGRAM_WEBHOOK_SECRET:
        print("TELEGRAM_WEBHOOK_SECRET must be set first.", file=sys.stderr)
        return 1
    async with TelegramNotifier() as notifier:
        result = await notifier.call_api(
            "setWebhook",
            {
                "url": public_url.rstrip("/") + "/webhooks/telegram",
                "secret_token": config.TELEGRAM_WEBHOOK_SECRET,
                "allowed_updates": ["message", "chat_member"],
                "drop_pending_updates": True,
            },
        )
    print("Telegram webhook registered." if result else "Failed to register the Telegram webhook.")
    return 0 if result else 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    config.configure_logging()

    if args and args[0] == "set-webhook" and len(args) == 2:
        return asyncio.run(_set_telegram_webhook(args[1]))
    if args and args[0] == "serve":
        import uvicorn

        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
        return 0

    print("Usage: python -m src.lemon_webhook serve | set-webhook <public-base-url>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
