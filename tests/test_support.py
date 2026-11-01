"""Support desk: tickets, canned answers, AI drafts, and the operator's commands."""
import pytest

from src import config
from src.database import SQLiteDatabase
from src.models import SUBSCRIBER_STATUS_ACTIVE, Subscriber
from src.support_bot import SupportService, match_faq, ticket_ref

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
    message = {"message_id": 8, "chat": {"id": OPERATOR, "type": "private"}, "from": {"id": OPERATOR}, **extra}
    if text is not None:
        message["text"] = text
    if reply_to is not None:
        message["reply_to_message"] = {"text": reply_to}
    return {"message": message}


async def fake_drafter(text):
    return "Hi Ada, we are looking into it."


@pytest.fixture
async def desk(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ADMIN_CHAT_ID", str(OPERATOR))
    monkeypatch.setattr(config, "SUPPORT_MAX_MESSAGES_PER_HOUR", 3)
    db = SQLiteDatabase(str(tmp_path / "s.db"))
    async with db:
        yield SupportService(db, FakeBot(), FakeBot())


async def first_ticket_text(desk, text="My dashboard shows an error when I open it"):
    await desk.handle_update(customer_message(text))
    await desk.wait_for_background()
    return next(p["text"] for p in desk.notifier.sent(OPERATOR) if p["text"].startswith("🎫"))


# --- customer side -----------------------------------------------------------
async def test_start_shows_the_english_welcome_and_does_not_open_a_ticket(desk):
    assert await desk.handle_update(customer_message("/start")) == "welcome"
    assert "Zepa Radar Support" in desk.notifier.sent(111)[0]["text"]
    assert desk.notifier.sent(OPERATOR) == []
    assert await desk.db.list_open_support_tickets() == []


async def test_customer_text_reaches_the_operator_with_a_ticket_header_and_plan(desk):
    await desk.db.upsert_subscriber(Subscriber("s1", "a@b.co", SUBSCRIBER_STATUS_ACTIVE, telegram_user_id=111))
    assert await desk.handle_update(customer_message("My dashboard shows an error")) == "forwarded"
    to_operator = desk.notifier.sent(OPERATOR)[0]["text"]
    assert "Ticket ZR-1 | user 111" in to_operator
    assert "Ada (@ada) · VIP active" in to_operator and "My dashboard shows an error" in to_operator
    assert "Your ticket is ZR-1" in desk.notifier.sent(111)[0]["text"]


async def test_messages_join_the_open_ticket_and_the_confirmation_is_not_repeated(desk):
    await desk.handle_update(customer_message("one"))
    await desk.handle_update(customer_message("two"))
    assert len(desk.notifier.sent(111)) == 1
    assert all("ZR-1 |" in p["text"] for p in desk.notifier.sent(OPERATOR))
    assert [m.body for m in await desk.db.get_support_messages(111)] == ["one", "two"]


async def test_photos_are_copied_to_the_operator_with_the_ticket_in_the_caption(desk):
    await desk.handle_update(customer_message(photo=[{"file_id": "x"}], caption="see screenshot"))
    method, payload = desk.notifier.calls[0]
    assert method == "copyMessage" and payload["from_chat_id"] == 111 and payload["message_id"] == 5
    assert "Ticket ZR-1" in payload["caption"] and "see screenshot" in payload["caption"]


async def test_stickers_and_other_content_are_politely_refused(desk):
    assert await desk.handle_update(customer_message(sticker={"file_id": "x"})) == "unsupported"
    assert desk.notifier.sent(OPERATOR) == []


async def test_a_flood_of_messages_is_cut_off(desk):
    results = [await desk.handle_update(customer_message(f"m{i}")) for i in range(5)]
    assert results == ["forwarded"] * 3 + ["rate_limited"] * 2
    assert len(desk.notifier.sent(OPERATOR)) == 3


async def test_when_the_operator_cannot_be_reached_the_customer_is_told_and_the_operator_warned(desk):
    desk.notifier.fail = True
    assert await desk.handle_update(customer_message("hello")) == "delivery_failed"
    assert "Start" in desk.alert_notifier.alerts[-1]


async def test_group_chats_and_other_updates_are_ignored(desk):
    assert await desk.handle_update(customer_message("hi", chat_type="supergroup")) == "ignored"
    assert await desk.handle_update({"edited_message": {}}) == "ignored"
    assert desk.notifier.calls == []


# --- canned answers ------------------------------------------------------------
def test_faq_matching_knows_the_common_questions_and_leaves_money_disputes_to_a_human():
    assert match_faq("How do I cancel my subscription?").name == "cancel"
    assert match_faq("I paid but I can't get in").name == "access"
    assert match_faq("Where do I find my invoice?").name == "billing"
    assert match_faq("I want a refund, please cancel") is None
    assert match_faq("The Software topic looks quiet today") is None


async def test_a_common_question_gets_a_canned_answer_and_still_reaches_the_operator(desk):
    assert await desk.handle_update(customer_message("I paid but I cannot get in")) == "auto_replied"
    answer = desk.notifier.sent(111)[0]["text"]
    assert "exact e-mail" in answer and "ZR-1" in answer
    assert "Auto-replied: access" in desk.notifier.sent(OPERATOR)[0]["text"]
    assert [m.direction for m in await desk.db.get_support_messages(111)] == ["customer", "auto"]


async def test_the_same_canned_answer_is_not_repeated_within_half_an_hour(desk):
    await desk.handle_update(customer_message("how do I cancel"))
    assert await desk.handle_update(customer_message("I said how do I cancel")) == "forwarded"
    assert "Auto-replied" not in desk.notifier.sent(OPERATOR)[1]["text"]


# --- AI drafts -------------------------------------------------------------------
async def test_an_ai_draft_goes_to_the_operator_only_and_is_sent_with_send(desk):
    desk.drafter = fake_drafter
    await desk.handle_update(customer_message("My dashboard shows an error when I open it"))
    await desk.wait_for_background()
    draft = next(p["text"] for p in desk.notifier.sent(OPERATOR) if p["text"].startswith("✍️"))
    assert "Draft for Ticket ZR-1 | user 111" in draft and "looking into it" in draft
    assert all("looking into it" not in p["text"] for p in desk.notifier.sent(111))  # never auto-sent

    desk.notifier.calls.clear()
    assert await desk.handle_update(operator_message("/send", reply_to=draft)) == "operator_replied"
    assert "Hi Ada, we are looking into it." in desk.notifier.sent(111)[0]["text"]


async def test_no_draft_for_tiny_messages_canned_answers_or_when_drafting_is_off(desk):
    desk.drafter = fake_drafter
    await desk.handle_update(customer_message("help"))
    await desk.handle_update(customer_message("how do I cancel my subscription", user_id=222))
    await desk.wait_for_background()
    assert not [p for p in desk.notifier.sent(OPERATOR) if p["text"].startswith("✍️")]


async def test_send_only_works_on_a_draft(desk):
    ticket_text = await first_ticket_text(desk)
    assert await desk.handle_update(operator_message("/send", reply_to=ticket_text)) == "operator_no_draft"
    assert desk.notifier.sent(111)[-1]["text"].startswith("✅ Got it")  # nothing new reached the customer


# --- operator commands -------------------------------------------------------------
async def test_the_operator_reply_goes_back_to_the_right_customer_and_is_logged(desk):
    ticket_text = await first_ticket_text(desk)
    desk.notifier.calls.clear()
    assert await desk.handle_update(operator_message("Fixed, try again", reply_to=ticket_text)) == "operator_replied"
    answer = desk.notifier.sent(111)[0]["text"]
    assert "Fixed, try again" in answer and "ZR-1" in answer
    assert "delivered" in desk.notifier.sent(OPERATOR)[0]["text"]
    assert (await desk.db.get_support_messages(111))[-1].direction == "operator"


async def test_the_operator_can_answer_with_a_file(desk):
    ticket_text = await first_ticket_text(desk)
    desk.notifier.calls.clear()
    update = operator_message(None, reply_to=ticket_text, photo=[{"file_id": "p"}])
    assert await desk.handle_update(update) == "operator_replied"
    method, payload = desk.notifier.calls[0]
    assert method == "copyMessage" and payload["chat_id"] == 111 and payload["from_chat_id"] == OPERATOR


async def test_an_operator_message_that_is_not_a_reply_gets_a_hint(desk):
    assert await desk.handle_update(operator_message("hello")) == "operator_no_ticket"
    assert "reply" in desk.notifier.sent(OPERATOR)[0]["text"].lower()
    assert desk.notifier.sent(111) == []


async def test_an_unknown_command_is_never_forwarded_to_the_customer(desk):
    ticket_text = await first_ticket_text(desk)
    desk.notifier.calls.clear()
    assert await desk.handle_update(operator_message("/whatever", reply_to=ticket_text)) == "operator_help"
    assert desk.notifier.sent(111) == []


async def test_a_blocked_customer_is_reported_when_a_reply_cannot_be_delivered(desk):
    ticket_text = await first_ticket_text(desk)
    desk.notifier.fail = True
    assert await desk.handle_update(operator_message("hi", reply_to=ticket_text)) == "operator_delivery_failed"


async def test_close_tells_the_customer_and_the_next_message_opens_a_new_ticket(desk):
    ticket_text = await first_ticket_text(desk)
    assert await desk.handle_update(operator_message("/close", reply_to=ticket_text)) == "operator_closed"
    assert "ZR-1 is closed" in desk.notifier.sent(111)[-1]["text"]
    assert await desk.db.list_open_support_tickets() == []
    assert await desk.handle_update(operator_message("/close", reply_to=ticket_text)) == "operator_already_closed"

    await desk.handle_update(customer_message("another problem with my account"))
    assert "Ticket ZR-2 | user 111" in desk.notifier.sent(OPERATOR)[-1]["text"]


async def test_close_refuses_a_ticket_that_belongs_to_someone_else(desk):
    await first_ticket_text(desk)
    forged = "🎫 Ticket ZR-1 | user 555"
    assert await desk.handle_update(operator_message("/close", reply_to=forged)) == "operator_no_ticket"
    assert len(await desk.db.list_open_support_tickets()) == 1


async def test_history_lists_the_customers_latest_messages(desk):
    ticket_text = await first_ticket_text(desk)
    await desk.handle_update(operator_message("Looking now", reply_to=ticket_text))
    desk.notifier.calls.clear()
    assert await desk.handle_update(operator_message("/history", reply_to=ticket_text)) == "operator_history"
    history = desk.notifier.sent(OPERATOR)[0]["text"]
    assert "👤" in history and "dashboard shows an error" in history and "🧑‍💼" in history and "Looking now" in history


async def test_block_silences_a_customer_and_unblock_restores_them(desk):
    ticket_text = await first_ticket_text(desk)
    assert await desk.handle_update(operator_message("/block", reply_to=ticket_text)) == "operator_blocked"
    desk.notifier.calls.clear()
    assert await desk.handle_update(customer_message("hello??")) == "blocked"
    assert desk.notifier.calls == []
    assert await desk.handle_update(operator_message("/unblock", reply_to=ticket_text)) == "operator_unblocked"
    assert await desk.handle_update(customer_message("hello again please")) == "forwarded"


async def test_open_lists_open_tickets(desk):
    assert await desk.handle_update(operator_message("/open")) == "operator_open"
    assert "No open tickets" in desk.notifier.sent(OPERATOR)[-1]["text"]
    await first_ticket_text(desk)
    await desk.handle_update(operator_message("/open"))
    assert "ZR-1 · Ada (@ada)" in desk.notifier.sent(OPERATOR)[-1]["text"]


# --- without the support tables --------------------------------------------------------
async def test_messages_still_flow_when_the_support_tables_are_missing(desk):
    async def broken(*args, **kwargs):
        raise RuntimeError("relation does not exist")

    for name in (
        "get_open_support_ticket", "create_support_ticket", "add_support_message", "is_support_blocked",
        "get_support_ticket", "get_support_messages", "set_support_blocked", "list_open_support_tickets",
    ):
        setattr(desk.db, name, broken)

    assert await desk.handle_update(customer_message("My dashboard shows an error")) == "forwarded"
    ticket_text = desk.notifier.sent(OPERATOR)[0]["text"]
    assert f"Ticket {ticket_ref(111)} | user 111" in ticket_text

    desk.notifier.calls.clear()
    assert await desk.handle_update(operator_message("On it", reply_to=ticket_text)) == "operator_replied"
    assert "On it" in desk.notifier.sent(111)[0]["text"]
    assert await desk.handle_update(operator_message("/close", reply_to=ticket_text)) == "operator_no_ticket"
    assert await desk.handle_update(operator_message("/history", reply_to=ticket_text)) == "operator_db_error"
    assert "support tables" in desk.notifier.sent(OPERATOR)[-1]["text"]


def test_ticket_fallback_reference_is_stable_per_customer_and_day():
    assert ticket_ref(111, 1_000_000) == ticket_ref(111, 1_000_500)
    assert ticket_ref(111, 1_000_000) != ticket_ref(222, 1_000_000)
    assert ticket_ref(111, 1_000_000) != ticket_ref(111, 1_000_000 + 86400)
    assert ticket_ref(111).startswith("ZR-") and len(ticket_ref(111)) == 8
