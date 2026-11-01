"""Support desk: customer -> operator tickets and operator -> customer replies."""
import pytest

from src import config
from src.database import SQLiteDatabase
from src.models import SUBSCRIBER_STATUS_ACTIVE, Subscriber
from src.support_bot import SupportService, ticket_ref

OPERATOR = 999


class FakeBot:
    """Records Bot API calls; `fail` makes every call report a delivery failure."""

    def __init__(self, fail=False):
        self.calls = []
        self.alerts = []
        self.fail = fail

    async def call_api(self, method, payload):
        self.calls.append((method, payload))
        return None if self.fail else {"ok": True}

    async def send_admin_alert(self, text, title=""):
        self.alerts.append(text)
        return True

    def sent(self, chat_id):
        return [p for m, p in self.calls if str(p.get("chat_id")) == str(chat_id)]


def customer_message(text=None, user_id=111, chat_type="private", **extra):
    message = {
        "message_id": 5,
        "chat": {"id": user_id, "type": chat_type},
        "from": {"id": user_id, "first_name": "Ada", "username": "ada"},
        **extra,
    }
    if text is not None:
        message["text"] = text
    return {"message": message}


def operator_message(text, reply_to=None, **extra):
    message = {"message_id": 8, "chat": {"id": OPERATOR, "type": "private"}, "from": {"id": OPERATOR}, "text": text, **extra}
    if reply_to is not None:
        message["reply_to_message"] = {"text": reply_to}
    return {"message": message}


@pytest.fixture
async def desk(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ADMIN_CHAT_ID", str(OPERATOR))
    monkeypatch.setattr(config, "SUPPORT_MAX_MESSAGES_PER_HOUR", 3)
    db = SQLiteDatabase(str(tmp_path / "s.db"))
    async with db:
        bot, main_bot = FakeBot(), FakeBot()
        yield SupportService(db, bot, main_bot)


def test_ticket_reference_is_stable_per_customer_and_day_and_differs_between_customers():
    assert ticket_ref(111, 1_000_000) == ticket_ref(111, 1_000_500)
    assert ticket_ref(111, 1_000_000) != ticket_ref(222, 1_000_000)
    assert ticket_ref(111, 1_000_000) != ticket_ref(111, 1_000_000 + 86400)
    assert ticket_ref(111, 1_000_000).startswith("ZR-") and len(ticket_ref(111)) == 8


async def test_start_shows_the_english_welcome_and_does_not_open_a_ticket(desk):
    assert await desk.handle_update(customer_message("/start")) == "welcome"
    assert "Zepa Radar Support" in desk.notifier.sent(111)[0]["text"]
    assert desk.notifier.sent(OPERATOR) == []


async def test_customer_text_reaches_the_operator_with_a_ticket_header_and_plan(desk):
    await desk.db.upsert_subscriber(Subscriber("s1", "a@b.co", SUBSCRIBER_STATUS_ACTIVE, telegram_user_id=111))
    assert await desk.handle_update(customer_message("My invite link expired")) == "forwarded"
    to_operator = desk.notifier.sent(OPERATOR)[0]["text"]
    assert f"Ticket {ticket_ref(111)} | user 111" in to_operator
    assert "Ada (@ada) · VIP active" in to_operator and "My invite link expired" in to_operator
    assert "Your ticket is" in desk.notifier.sent(111)[0]["text"]


async def test_the_confirmation_is_not_repeated_for_every_message(desk):
    await desk.handle_update(customer_message("one"))
    await desk.handle_update(customer_message("two"))
    assert len(desk.notifier.sent(111)) == 1
    assert len(desk.notifier.sent(OPERATOR)) == 2


async def test_photos_are_copied_to_the_operator_with_the_ticket_in_the_caption(desk):
    await desk.handle_update(customer_message(photo=[{"file_id": "x"}], caption="see screenshot"))
    method, payload = desk.notifier.calls[0]
    assert method == "copyMessage" and payload["from_chat_id"] == 111 and payload["message_id"] == 5
    assert "Ticket ZR-" in payload["caption"] and "see screenshot" in payload["caption"]


async def test_stickers_and_other_content_are_politely_refused(desk):
    assert await desk.handle_update(customer_message(sticker={"file_id": "x"})) == "unsupported"
    assert desk.notifier.sent(OPERATOR) == []


async def test_a_flood_of_messages_is_cut_off(desk):
    results = [await desk.handle_update(customer_message(f"m{i}")) for i in range(5)]
    assert results == ["forwarded"] * 3 + ["rate_limited"] * 2
    assert len(desk.notifier.sent(OPERATOR)) == 3


async def test_the_operator_reply_goes_back_to_the_right_customer(desk):
    await desk.handle_update(customer_message("help"))
    ticket_text = desk.notifier.sent(OPERATOR)[0]["text"]
    desk.notifier.calls.clear()
    assert await desk.handle_update(operator_message("Fixed, try again", reply_to=ticket_text)) == "operator_replied"
    answer = desk.notifier.sent(111)[0]["text"]
    assert "Fixed, try again" in answer and ticket_ref(111) in answer
    assert "delivered" in desk.notifier.sent(OPERATOR)[0]["text"]


async def test_the_operator_can_answer_with_a_file(desk):
    ticket_text = f"🎫 Ticket {ticket_ref(111)} | user 111\nAda"
    update = operator_message("", reply_to=ticket_text, photo=[{"file_id": "p"}])
    update["message"].pop("text")
    assert await desk.handle_update(update) == "operator_replied"
    method, payload = desk.notifier.calls[0]
    assert method == "copyMessage" and payload["chat_id"] == 111 and payload["from_chat_id"] == OPERATOR


async def test_an_operator_message_that_is_not_a_reply_gets_a_hint(desk):
    assert await desk.handle_update(operator_message("hello")) == "operator_no_ticket"
    assert "reply" in desk.notifier.sent(OPERATOR)[0]["text"].lower()
    assert desk.notifier.sent(111) == []


async def test_a_blocked_customer_is_reported_to_the_operator(desk):
    desk.notifier.fail = True
    ticket_text = f"🎫 Ticket {ticket_ref(111)} | user 111"
    assert await desk.handle_update(operator_message("hi", reply_to=ticket_text)) == "operator_delivery_failed"


async def test_when_the_operator_cannot_be_reached_the_customer_is_told_and_the_operator_warned(desk):
    desk.notifier.fail = True
    assert await desk.handle_update(customer_message("hello")) == "delivery_failed"
    assert "Start" in desk.alert_notifier.alerts[-1]


async def test_group_chats_and_other_updates_are_ignored(desk):
    assert await desk.handle_update(customer_message("hi", chat_type="supergroup")) == "ignored"
    assert await desk.handle_update({"edited_message": {}}) == "ignored"
    assert desk.notifier.calls == []
