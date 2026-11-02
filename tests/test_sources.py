"""Source switches, duplicate handling, pre-filter and the Hacker News / job-board rules."""
import time
from datetime import datetime, timezone

import pytest

from src import config, scrapers
from src.models import RawLead
from src.scrapers.base_scraper import BaseScraper
from src.scrapers.hn_scraper import HackerNewsScraper
from src.scrapers.jobboards_scraper import JobBoardsScraper


def raw(item_id, title, source="reddit"):
    return RawLead(item_id=item_id, source=source, title=title, url="https://x", body="[HIRING] body text here")


def fake_scraper(name, leads, fail=False):
    class Fake(BaseScraper):
        def __init__(self, *args, rotate=False, throttle=False, **kwargs):
            super().__init__(*args, **kwargs)

        async def _scrape(self):
            if fail:
                raise RuntimeError("boom")
            return list(leads)

    Fake.name = name
    return Fake


async def test_disabled_source_is_skipped_and_the_others_keep_running(monkeypatch):
    registry = {
        "freelancer": fake_scraper("freelancer", [raw("freelancer:1", "[HIRING] a project title long enough", "freelancer")]),
        "reddit": fake_scraper("reddit", [raw("reddit:1", "[HIRING] another project title that is long")]),
    }
    monkeypatch.setattr(scrapers, "SCRAPER_CLASSES", registry)
    monkeypatch.setattr(config, "DISABLED_SOURCES", frozenset({"freelancer"}))
    leads = await scrapers.scrape_all()
    assert {lead.source for lead in leads} == {"reddit"}


async def test_a_crashing_source_does_not_stop_the_rest(monkeypatch):
    registry = {
        "freelancer": fake_scraper("freelancer", [], fail=True),
        "reddit": fake_scraper("reddit", [raw("reddit:1", "[HIRING] a project title that is long enough")]),
    }
    monkeypatch.setattr(scrapers, "SCRAPER_CLASSES", registry)
    monkeypatch.setattr(config, "DISABLED_SOURCES", frozenset())
    assert [lead.item_id for lead in await scrapers.scrape_all()] == ["reddit:1"]


async def test_same_title_posted_twice_is_kept_once(monkeypatch):
    title = "[HIRING] Video editor for weekly YouTube channel"
    registry = {"reddit": fake_scraper("reddit", [raw("reddit:1", title), raw("reddit:2", title.upper())])}
    monkeypatch.setattr(scrapers, "SCRAPER_CLASSES", registry)
    monkeypatch.setattr(config, "DISABLED_SOURCES", frozenset())
    assert len(await scrapers.scrape_all()) == 1


def test_prefilter_needs_hiring_intent():
    assert config.passes_prefilter("[HIRING] Video editor, $300/video")
    assert config.passes_prefilter("Looking for a developer to build a Shopify app")
    assert not config.passes_prefilter("Bootstrapped SaaS at 180k ARR. How would you scale?", "we spent $5000 on a project")
    assert not config.passes_prefilter("[FOR HIRE] I edit videos")
    assert config.passes_prefilter("Senior platform engineer wanted", "", trusted_source=True)


@pytest.mark.parametrize(
    "title",
    [
        "[ FOR HIRE ] Illustrator and character designer, Looking for work (DM me)",
        "Title: [For Hire] Pixel art environments + UI + Godot dev + writer looking for an indie team",
        "Video Editor [FOR HIRE] with 5 years of experience",
        "Hello, I am an audio engineer/sound designer looking for immediate work",
        "Beginner Editor looking for Work",
        "Looking for clients - Social Media Content Creator & Marketing Specialist",
        "I am looking for a client who needs video editing services",
        "Looking for long-term opportunities as a Virtual Assistant",
    ],
)
def test_prefilter_drops_freelancers_advertising_themselves(title):
    assert not config.passes_prefilter(title)


@pytest.mark.parametrize(
    "title",
    [
        "[HIRING] Long-form Gaming Editor | $100-150/Video",
        "Looking for a developer to work on my project",
        "Looking for a project manager for a SaaS launch",
        "Looking for a video editor who can start immediately",
        "Looking for work-from-home data entry help, $15/hour",
        "[hiring] Looking for ongoing thumbnail designer - $30 per thumbnail",
    ],
)
def test_prefilter_keeps_real_clients_that_say_looking_for(title):
    assert config.passes_prefilter(title)


def test_job_board_titles_are_not_judged_by_the_job_seeker_rule():
    assert config.passes_prefilter("Contract designer - open to work across time zones", "", trusted_source=True)


def hn_comment(thread, text):
    return {"objectID": "1", "created_at_i": int(time.time()), "_tags": ["comment"],
            "story_title": thread, "comment_text": text}


def test_hn_keeps_clients_and_drops_freelancers_in_the_mixed_thread():
    hn, thread = HackerNewsScraper(), "Ask HN: Freelancer? Seeking freelancer? (November 2026)"
    assert hn._to_lead(hn_comment(thread, "SEEKING FREELANCER | Acme | Remote<p>React dev")) is not None
    assert hn._to_lead(hn_comment(thread, "SEEKING WORK | Berlin<p>React dev here")) is None


def test_hn_ignores_comments_outside_hiring_threads():
    assert HackerNewsScraper()._to_lead(hn_comment("Show HN: my camera", "I am looking for a camera")) is None


def test_arbeitnow_is_off_by_default():
    assert "arbeitnow" not in config.ENABLED_JOB_BOARDS


def test_jobicy_is_throttled_to_once_an_hour(monkeypatch):
    from src.scrapers import jobboards_scraper as jb

    scraper = JobBoardsScraper(throttle=True)
    for minute, due in ((5, True), (19, True), (20, False), (45, False)):
        monkeypatch.setattr(jb, "utc_now", lambda m=minute: datetime(2026, 1, 1, 12, m, tzinfo=timezone.utc))
        assert scraper._jobicy_due() is due
    assert JobBoardsScraper(throttle=False)._jobicy_due() is True


def test_freelancer_off_topic_skills_are_dropped():
    from src.scrapers.freelancer_scraper import FreelancerScraper

    project = {"id": 1, "title": "Residential HVAC Coordination Drawings", "seo_url": "x/y", "status": "active",
               "time_submitted": time.time(), "currency": {"code": "USD", "exchange_rate": 1},
               "budget": {"minimum": 250, "maximum": 750}, "type": "fixed",
               "jobs": [{"name": "Architecture"}, {"name": "Graphic Design"}], "description": "design"}
    assert FreelancerScraper()._to_lead(project) is None
    project["jobs"] = [{"name": "Graphic Design"}]
    assert FreelancerScraper()._to_lead(project) is not None
