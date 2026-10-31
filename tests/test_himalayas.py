"""Himalayas scraper: only contract-like roles, honest pay handling, paging and polling cadence."""
import time
from datetime import datetime, timezone

from src import config
from src.scrapers import SCRAPER_CLASSES, himalayas_scraper
from src.scrapers.himalayas_scraper import HimalayasScraper
from src.scoring import BUDGET_EXPLICIT, score_lead


def job(guid="https://himalayas.app/companies/acme/jobs/dev-1", employment="Contractor", minutes_old=60, **extra):
    base = {
        "title": "Senior React Developer (Contract)", "excerpt": "Short text", "companyName": "Acme",
        "employmentType": employment, "minSalary": None, "maxSalary": None, "salaryPeriod": "annual",
        "currency": None, "locationRestrictions": [], "categories": ["React-Developer"],
        "parentCategories": ["Engineering"], "description": "<p>Build a dashboard.</p><ul><li>React</li></ul>",
        "pubDate": str(int(time.time()) - minutes_old * 60), "applicationLink": guid, "guid": guid,
    }
    return base | extra


def test_only_contract_part_time_and_temporary_roles_are_kept():
    scraper = HimalayasScraper()
    assert scraper._to_lead(job(employment="Full Time")) is None
    assert scraper._to_lead(job(employment="Intern")) is None
    for kind in ("Contractor", "Part Time", "Temporary"):
        assert scraper._to_lead(job(employment=kind)) is not None


def test_lead_links_to_the_unchanged_application_link():
    lead = HimalayasScraper()._to_lead(job())
    assert lead.url == "https://himalayas.app/companies/acme/jobs/dev-1"
    assert lead.source == "himalayas" and lead.item_id.startswith("himalayas:")
    assert "Employment type: Contractor" in lead.body and "worldwide" in lead.body
    assert "Build a dashboard." in lead.body and "<p>" not in lead.body


def test_hourly_usd_rate_becomes_an_explicit_budget():
    lead = HimalayasScraper()._to_lead(job(minSalary=85, maxSalary=185, salaryPeriod="hourly", currency="USD"))
    assert lead.extra["budget_kind"] == BUDGET_EXPLICIT and lead.extra["type"] == "hourly"
    assert "$85 - $185 per hour" in lead.body
    facts = {"engagement": "ongoing_contract", "scope_clarity": 4, "remote_digital": True, "geo_restriction": "worldwide"}
    assert score_lead(lead, facts).facts["budget_kind"] == BUDGET_EXPLICIT


def test_annual_or_foreign_salaries_are_not_used_as_a_project_budget():
    scraper = HimalayasScraper()
    for extra in ({"minSalary": 90000, "maxSalary": 120000, "salaryPeriod": "annual", "currency": "USD"},
                  {"minSalary": 40, "maxSalary": 60, "salaryPeriod": "hourly", "currency": "EUR"}):
        lead = scraper._to_lead(job(**extra))
        assert "budget_usd_max" not in lead.extra and "Pay:" not in lead.body


def test_location_restrictions_are_passed_on_for_the_geo_check():
    lead = HimalayasScraper()._to_lead(job(locationRestrictions=["United States", "Canada"]))
    assert "Location restrictions: United States, Canada" in lead.body


def test_stale_jobs_are_dropped():
    assert HimalayasScraper()._to_lead(job(minutes_old=60 * 24 * 3)) is None


async def test_paging_follows_the_cursor_and_stops_at_a_stale_page(monkeypatch):
    pages = {
        None: {"jobs": [job(f"g{i}") for i in range(3)], "nextCursor": "c1"},
        "c1": {"jobs": [job(f"h{i}") for i in range(2)], "nextCursor": "c2"},
        "c2": {"jobs": [job(f"old{i}", minutes_old=60 * 24 * 5) for i in range(2)], "nextCursor": "c3"},
        "c3": {"jobs": [job("never")], "nextCursor": None},
    }
    calls = []

    async def fake_fetch_json(self, url, params=None, headers=None):
        cursor = (params or {}).get("cursor")
        calls.append(cursor)
        return pages[cursor]

    monkeypatch.setattr(HimalayasScraper, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(config, "HIMALAYAS_MAX_PAGES", 10)
    leads = await HimalayasScraper().scrape()
    assert len(leads) == 5 and calls == [None, "c1", "c2"]  # the stale page ends the loop


async def test_page_limit_is_respected(monkeypatch):
    async def fake_fetch_json(self, url, params=None, headers=None):
        return {"jobs": [job(f"x{(params or {}).get('cursor')}")], "nextCursor": "again"}

    monkeypatch.setattr(HimalayasScraper, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(config, "HIMALAYAS_MAX_PAGES", 2)
    assert len(await HimalayasScraper().scrape()) == 2


def test_polling_cadence_only_applies_to_scheduled_runs(monkeypatch):
    for hour, minute, due in ((0, 5, True), (3, 19, True), (3, 20, False), (4, 5, False), (6, 0, True)):
        monkeypatch.setattr(himalayas_scraper, "utc_now", lambda h=hour, m=minute: datetime(2026, 1, 1, h, m, tzinfo=timezone.utc))
        assert HimalayasScraper(throttle=True)._due() is due
        assert HimalayasScraper(throttle=False)._due() is True


def test_source_is_registered_and_trusted():
    assert "himalayas" in SCRAPER_CLASSES and "himalayas" in config.TRUSTED_HIRING_SOURCES


def test_reddit_direct_jobs_group_has_the_new_communities():
    group = config.REDDIT_GROUPS["direct_jobs"].lower().split("+")
    for sub in ("hiring", "gamedevjobs", "gamedevclassifieds", "remotejobs", "seojobs", "pythonjobs", "webdevjobs", "hireawriter"):
        assert sub in group
    assert "artcommissions" not in group  # all for-hire art posts, no clients
