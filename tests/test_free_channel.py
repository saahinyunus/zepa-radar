"""Free channel: one batch every few hours, closed by a Join VIP message."""
from datetime import datetime, timezone

import main
from src import config
from src.telegram_notifier import TelegramNotifier, format_free_cta


def at(hour):
    return datetime(2026, 1, 1, hour, 30, tzinfo=timezone.utc)


def test_batches_only_run_in_every_nth_hour(monkeypatch):
    monkeypatch.setattr(config, "FREE_POST_EVERY_HOURS", 3)
    assert [h for h in range(24) if main.free_batch_due(at(h))] == [0, 3, 6, 9, 12, 15, 18, 21]
    monkeypatch.setattr(config, "FREE_POST_EVERY_HOURS", 0)
    assert all(main.free_batch_due(at(h)) for h in range(24))


class FakeDb:
    def __init__(self):
        self.sent = []

    async def mark_sent_free(self, item_id):
        self.sent.append(item_id)


class FakeLead:
    def __init__(self, item_id):
        self.item_id = item_id


def make_notifier(monkeypatch, outcomes):
    notifier = TelegramNotifier.__new__(TelegramNotifier)
    notifier.free_channel_id = "-100free"
    notifier.messages = []

    async def send_free_lead(lead):
        return outcomes[lead.item_id]

    async def send_message(chat_id, text, *, thread_id=None, keyboard=None):
        notifier.messages.append((text, keyboard))
        return True

    notifier.send_free_lead = send_free_lead
    notifier._send_message = send_message
    monkeypatch.setattr(config, "TELEGRAM_SEND_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(config, "LEMON_SQUEEZY_CHECKOUT_URL", "https://shop.example/checkout/buy/x")
    return notifier


async def test_join_vip_message_closes_a_batch_that_sent_something(monkeypatch):
    notifier = make_notifier(monkeypatch, {"a": True, "b": False})
    db = FakeDb()
    assert await notifier.send_free_leads([FakeLead("a"), FakeLead("b")], db) == 1
    assert db.sent == ["a"]
    text, keyboard = notifier.messages[-1]
    assert text == format_free_cta()
    assert keyboard["inline_keyboard"][0][0]["url"] == "https://shop.example/checkout/buy/x"


async def test_no_join_message_when_nothing_was_sent(monkeypatch):
    notifier = make_notifier(monkeypatch, {"a": False})
    assert await notifier.send_free_leads([FakeLead("a")], FakeDb()) == 0
    assert notifier.messages == []


def test_missing_free_channel_settings_are_reported_but_not_fatal(monkeypatch):
    monkeypatch.setattr(config, "FREE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "TELEGRAM_FREE_CHANNEL_ID", "")
    monkeypatch.setattr(config, "LEMON_SQUEEZY_CHECKOUT_URL", "")
    problems = config.free_channel_problems()
    assert len(problems) == 2
    # validate_config still lists them, and main.amain filters exactly these out of its fatal list
    assert set(problems) <= set(config.validate_config(config.MODE_TELEGRAM_SAAS))
    monkeypatch.setattr(config, "FREE_CHANNEL_ENABLED", False)
    assert config.free_channel_problems() == []
