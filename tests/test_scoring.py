"""Scoring v2: deterministic, explainable, and free of the biases found in the first audit."""
from datetime import timedelta

import pytest

from src.models import RawLead, utc_now
from src.scoring import (
    BUDGET_EXPLICIT, BUDGET_PLATFORM_RANGE, BUDGET_UNKNOWN, budget_points, competition_points,
    parse_amount_usd, representative_amount, score_lead,
)

GOOD = {"engagement": "one_off_project", "scope_clarity": 4, "remote_digital": True,
        "geo_restriction": "worldwide", "red_flags": []}


def lead(source="reddit", title="[HIRING] Video editor", body="Need an editor.", minutes_old=20, **extra):
    return RawLead(
        item_id=f"{source}:t", source=source, title=title, url="https://example.com", body=body,
        posted_at=utc_now() - timedelta(minutes=minutes_old), extra=extra,
    )


# --- the three cases the external review asked for -------------------------------------------
def test_generic_freelancer_preset_range_no_longer_passes():
    raw = lead("freelancer", "Fix my site", "x" * 400, minutes_old=240,
               budget_usd_min=250, budget_usd_max=750, type="fixed", bids=23)
    result = score_lead(raw, {**GOOD, "scope_clarity": 3})
    assert result.facts["budget_kind"] == BUDGET_PLATFORM_RANGE
    assert result.score < 60


def test_reddit_post_without_budget_but_clear_retainer_passes():
    raw = lead("reddit", "[HIRING] Editor for my YouTube channel (200k subs), weekly videos",
               "Ongoing monthly work, 4 long-form videos per month.", minutes_old=25)
    result = score_lead(raw, {**GOOD, "engagement": "ongoing_contract", "scope_clarity": 5})
    assert result.facts["budget_kind"] == BUDGET_UNKNOWN
    assert result.score >= 60


def test_onsite_job_is_dropped():
    result = score_lead(lead("jobicy"), {**GOOD, "remote_digital": False, "geo_restriction": "specific_country"})
    assert result.score == 0 and not result.valid


# --- budget handling --------------------------------------------------------------------------
def test_preset_range_is_scored_by_geometric_mean_not_by_its_upper_end():
    assert representative_amount(250, 750) == pytest.approx(433, abs=1)
    assert budget_points(representative_amount(250, 750), hourly=False) == 12  # not 17 (the 750 tier)


def test_platform_range_stays_below_an_explicit_budget_of_the_same_size():
    preset = score_lead(lead('freelancer', budget_usd_min=1500, budget_usd_max=3000, type='fixed', bids=3), GOOD)
    explicit = score_lead(lead(body='Budget is $3,000 fixed.'), GOOD)
    assert preset.parts['budget'] < explicit.parts['budget']


def test_platform_range_never_earns_top_tier():
    raw = lead("freelancer", budget_usd_min=5000, budget_usd_max=10000, type="fixed", bids=3)
    assert score_lead(raw, GOOD).parts["budget"] <= 15


def test_explicit_amount_in_text_is_used_and_hourly_is_detected():
    amount, hourly = parse_amount_usd("pay $50/hr or $5k flat")
    assert (amount, hourly) == (5000.0, True)
    assert parse_amount_usd("no money here") == (None, False)


def test_unknown_budget_is_neutral_not_a_reward():
    result = score_lead(lead(), GOOD)
    assert result.facts["budget_kind"] == BUDGET_UNKNOWN
    assert result.parts["budget"] == 10


def test_explicit_budget_in_text_beats_unknown():
    with_budget = score_lead(lead(body="Budget $1,500 fixed for the project."), GOOD)
    assert with_budget.facts["budget_kind"] == BUDGET_EXPLICIT
    assert with_budget.parts["budget"] > score_lead(lead(), GOOD).parts["budget"]


# --- competition and freshness ------------------------------------------------------------------
def test_bidding_platform_points_fall_with_bids_and_direct_contact_is_flat():
    assert competition_points("freelancer", 3) > competition_points("freelancer", 25) > competition_points("freelancer", 100)
    assert competition_points("freelancer", None) < competition_points("reddit", None)
    assert competition_points("reddit", None) == competition_points("hackernews", None)


def test_unknown_posting_time_is_neutral():
    raw = lead()
    raw.posted_at = None
    assert score_lead(raw, GOOD).parts["fresh"] == 5


def test_older_posts_score_lower():
    assert score_lead(lead(minutes_old=10), GOOD).score > score_lead(lead(minutes_old=300), GOOD).score


# --- eligibility, flags, geography ----------------------------------------------------------------
@pytest.mark.parametrize("flag", ["pay_to_apply", "scam_or_mlm", "adult_or_illegal", "policy_violating"])
def test_fatal_flags_disqualify(flag):
    result = score_lead(lead(), {**GOOD, "red_flags": [flag]})
    assert result.score == 0 and not result.valid


def test_soft_flags_cost_points_each():
    base = score_lead(lead(), GOOD).score
    assert score_lead(lead(), {**GOOD, "red_flags": ["spec_work"]}).score == base - 12
    assert score_lead(lead(), {**GOOD, "red_flags": ["spec_work", "commission_only"]}).score == base - 24


def test_unknown_flag_names_are_ignored():
    assert score_lead(lead(), {**GOOD, "red_flags": ["made_up"]}).score == score_lead(lead(), GOOD).score


def test_country_bound_job_is_penalised_and_worldwide_is_not():
    worldwide = score_lead(lead(), GOOD).score
    assert score_lead(lead(), {**GOOD, "geo_restriction": "specific_country"}).score == worldwide - 25
    assert score_lead(lead(), {**GOOD, "geo_restriction": "us_only"}).score == worldwide - 8
    assert score_lead(lead(), {**GOOD, "geo_restriction": "nonsense"}).score == worldwide


def test_missing_remote_flag_is_not_treated_as_onsite():
    facts = {k: v for k, v in GOOD.items() if k != "remote_digital"}
    assert score_lead(lead(), facts).valid


def test_full_time_role_is_capped():
    raw = lead("remoteok", body="Salary $150k. " + "x" * 800)
    result = score_lead(raw, {**GOOD, "engagement": "full_time_job", "scope_clarity": 5})
    assert result.score <= 55


def test_garbage_facts_do_not_crash():
    result = score_lead(lead(), {"engagement": 7, "scope_clarity": "high", "red_flags": "x", "geo_restriction": None})
    assert 0 <= result.score <= 100


# --- explainability ---------------------------------------------------------------------------
def test_result_carries_a_breakdown_that_adds_up():
    result = score_lead(lead(), GOOD)
    assert sum(result.parts.values()) == result.score
    assert result.facts["v"] == 2 and result.facts["parts"] == result.parts
    assert "base=" in result.describe()


def test_score_is_deterministic():
    raw = lead()
    assert score_lead(raw, GOOD).score == score_lead(raw, GOOD).score
