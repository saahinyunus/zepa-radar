"""Facts storage, the SQLite store and the daily VIP cap."""
from datetime import timedelta

import pytest

import main
from src import config
from src.database import SQLiteDatabase
from src.models import AnalyzedLead, utc_now
from src.telegram_notifier import format_free_message, format_vip_message


def make_lead(item_id="reddit:1", score=80, **kwargs):
    return AnalyzedLead(item_id=item_id, source="reddit", category="SOFTWARE", title="T", url="https://x",
                        quality_score=score, estimated_budget="$500", summary_en="Two sentences.",
                        client_pain_point="Needs X.", **kwargs)


def test_facts_roundtrip_through_the_db_row():
    facts = {"v": 2, "engagement": "one_off_project", "parts": {"base": 10}}
    row = make_lead(facts=facts).to_db_row()
    assert row["pitch_template_en"].startswith("{")
    restored = AnalyzedLead.from_db_row(row)
    assert restored.facts == facts and restored.winning_pitch_en == ""


def test_old_rows_with_a_plain_text_pitch_still_load():
    row = make_lead().to_db_row() | {"pitch_template_en": "Hi there, I saw your post about X."}
    restored = AnalyzedLead.from_db_row(row)
    assert restored.facts == {} and restored.winning_pitch_en.startswith("Hi there")


def test_broken_facts_json_is_ignored():
    row = make_lead().to_db_row() | {"pitch_template_en": "{not json"}
    assert AnalyzedLead.from_db_row(row).facts == {}


def test_telegram_messages_have_no_pitch():
    lead = make_lead(facts={"v": 2})
    for message in (format_vip_message(lead), format_free_message(lead)):
        assert "pitch" not in message.lower()
    assert "Budget" in format_vip_message(lead)


@pytest.fixture
async def db(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "t.db"))
    async with database:
        yield database


async def test_count_vip_sent_since_counts_only_sent_leads_after_the_cutoff(db):
    for i in range(3):
        await db.save_lead(make_lead(f"reddit:{i}"))
    await db.mark_sent_vip("reddit:0")
    await db.mark_sent_vip("reddit:1")
    assert await db.count_vip_sent_since(utc_now() - timedelta(hours=1)) == 2
    assert await db.count_vip_sent_since(utc_now() + timedelta(hours=1)) == 0


async def test_daily_cap_limits_the_run_allowance(db, monkeypatch):
    monkeypatch.setattr(config, "VIP_MAX_PER_RUN", 4)
    monkeypatch.setattr(config, "VIP_DAILY_CAP", 5)
    assert await main.vip_allowance(db) == 4
    for i in range(3):
        await db.save_lead(make_lead(f"reddit:{i}"))
        await db.mark_sent_vip(f"reddit:{i}")
    assert await main.vip_allowance(db) == 2  # 5 - 3 already sent today
    for i in range(3, 6):
        await db.save_lead(make_lead(f"reddit:{i}"))
        await db.mark_sent_vip(f"reddit:{i}")
    assert await main.vip_allowance(db) == 0  # cap reached: nothing is posted to fill the quota


async def test_daily_cap_can_be_disabled(db, monkeypatch):
    monkeypatch.setattr(config, "VIP_DAILY_CAP", 0)
    monkeypatch.setattr(config, "VIP_MAX_PER_RUN", 4)
    assert await main.vip_allowance(db) == 4


async def test_pending_vip_leads_come_best_score_first(db, monkeypatch):
    monkeypatch.setattr(config, "VIP_MIN_SCORE", 60)
    for item_id, score in (("a:1", 65), ("a:2", 90), ("a:3", 75), ("a:4", 40)):
        await db.save_lead(make_lead(item_id, score))
    pending = await db.get_pending_vip_leads(limit=3)
    assert [lead.item_id for lead in pending] == ["a:2", "a:3", "a:1"]
