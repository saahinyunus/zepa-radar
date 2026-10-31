"""Lemon Squeezy webhook logic: signatures, test-mode guard, and the subscription lifecycle."""
import hashlib
import hmac
import json
from datetime import timedelta

import pytest

from src import config, lemon_webhook
from src.database import SQLiteDatabase
from src.lemon_webhook import SubscriptionService, parse_event, verify_signature
from src.models import utc_now


class FakeNotifier:
    """Records Bot API calls instead of contacting Telegram."""

    vip_group_id = "-100123"

    def __init__(self):
        self.calls = []

    async def call_api(self, method, payload):
        self.calls.append((method, payload))
        if method == "createChatInviteLink":
            return {"invite_link": f"https://t.me/+invite{len(self.calls)}"}
        return {"ok": True}


def payload(event, sub_id="42", email="Buyer@Example.com", status="active", ends_at=None, test_mode=False):
    return {
        "meta": {"event_name": event, "test_mode": test_mode},
        "data": {"type": "subscriptions", "id": sub_id,
                 "attributes": {"user_email": email, "status": status, "ends_at": ends_at}},
    }


@pytest.fixture
async def service(tmp_path):
    db = SQLiteDatabase(str(tmp_path / "w.db"))
    async with db:
        yield SubscriptionService(db, FakeNotifier())


def test_signature_check():
    body = b'{"a": 1}'
    good = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_signature(body, good, "secret")
    assert not verify_signature(body, good, "other")
    assert not verify_signature(body, None, "secret")
    assert not verify_signature(body, good, "")


def test_parse_event_reads_test_mode_and_normalises_email():
    event = parse_event(payload("subscription_created", test_mode=True))
    assert event.test_mode is True and event.email == "buyer@example.com"
    assert parse_event(payload("subscription_created")).test_mode is False
    assert parse_event({"meta": {"event_name": "order_created"}, "data": {}}) is None


async def test_test_mode_events_are_ignored_in_a_live_store(service, monkeypatch):
    monkeypatch.setattr(config, "LEMON_SQUEEZY_ALLOW_TEST_MODE", False)
    result = await service.handle_event(parse_event(payload("subscription_created", test_mode=True)))
    assert result == "ignored_test_mode"
    assert await service.db.get_subscriber("42") is None
    assert service.notifier.calls == []  # no invite link was created


async def test_test_mode_events_work_when_explicitly_allowed(service, monkeypatch):
    monkeypatch.setattr(config, "LEMON_SQUEEZY_ALLOW_TEST_MODE", True)
    assert await service.handle_event(parse_event(payload("subscription_created", test_mode=True))) == "activated"


async def test_live_purchase_creates_an_active_subscriber_with_an_invite_link(service, monkeypatch):
    monkeypatch.setattr(config, "LEMON_SQUEEZY_ALLOW_TEST_MODE", False)
    assert await service.handle_event(parse_event(payload("subscription_created"))) == "activated"
    subscriber = await service.db.get_subscriber("42")
    assert subscriber.status == "active" and subscriber.invite_link.startswith("https://t.me/+")


async def test_cancel_keeps_access_until_the_period_ends_then_expiry_removes(service):
    await service.handle_event(parse_event(payload("subscription_created")))
    future = (utc_now() + timedelta(days=10)).isoformat()
    result = await service.handle_event(parse_event(payload("subscription_cancelled", status="cancelled", ends_at=future)))
    assert result == "cancelled_until_period_end"
    assert (await service.db.get_subscriber("42")).status == "cancelled"
    assert await service.handle_event(parse_event(payload("subscription_expired", status="expired"))) == "expired"
    assert (await service.db.get_subscriber("42")).status == "expired"


async def test_email_claim_binds_the_telegram_account_and_blocks_a_second_one(service):
    await service.handle_event(parse_event(payload("subscription_created")))
    assert "Subscription found" in await service.claim_by_email(111, "buyer@example.com")
    assert (await service.db.get_subscriber("42")).telegram_user_id == 111
    assert "already linked" in await service.claim_by_email(222, "buyer@example.com")


async def test_claim_without_a_subscription_points_to_checkout(service, monkeypatch):
    monkeypatch.setattr(config, "LEMON_SQUEEZY_CHECKOUT_URL", "https://shop.example/checkout/buy/x")
    reply = await service.claim_by_email(5, "nobody@example.com")
    assert "couldn't find" in reply and "https://shop.example/checkout/buy/x" in reply


def test_the_module_still_imports_the_app():
    assert hasattr(lemon_webhook, "app")
