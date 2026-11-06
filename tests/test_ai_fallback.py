"""Triage runs on its own model and moves to a fallback when that model is down or out of quota."""
import json

import httpx
import pytest

from src import config
from src.ai_analyzer import AIAnalyzer
from src.models import RawLead

TRIAGE_REPLY = {"is_actual_client_hiring": False, "category": "OTHER", "score": 10}


def lead(n=1):
    return RawLead(item_id=f"reddit:{n}", source="reddit", title="[HIRING] video editor for my channel", url="https://x", body="body")


def make_analyzer(behaviour, **kwargs):
    """`behaviour(model) -> httpx.Response`; returns the analyzer and the list of models called."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        return behaviour(model)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    options = dict(api_key="k", model="main", fallback_model="", triage_model="tri", triage_fallback_model="tri-fb", max_calls=10)
    return AIAnalyzer(client, **{**options, **kwargs}), calls


def ok():
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(TRIAGE_REPLY)}}], "usage": {"total_tokens": 300}})


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(config, "backoff_delay", lambda attempt: 0.0)


async def test_triage_model_down_moves_to_the_fallback_and_stays_there():
    analyzer, calls = make_analyzer(lambda model: httpx.Response(503) if model == "tri" else ok())
    first = await analyzer.analyze(lead(1))
    second = await analyzer.analyze(lead(2))
    assert first is not None and second is not None
    assert analyzer.failures == 0
    assert calls == ["tri"] * config.HTTP_MAX_RETRIES + ["tri-fb", "tri-fb"]


async def test_triage_model_down_without_a_fallback_is_a_failure():
    analyzer, calls = make_analyzer(lambda model: httpx.Response(503), triage_fallback_model="")
    assert await analyzer.analyze(lead()) is None
    assert analyzer.failures == 1
    assert set(calls) == {"tri"}


async def test_a_timeout_counts_as_the_model_being_down():
    def behaviour(model):
        if model == "tri":
            raise httpx.ConnectTimeout("slow")
        return ok()

    analyzer, calls = make_analyzer(behaviour)
    assert await analyzer.analyze(lead()) is not None
    assert calls[-1] == "tri-fb"


async def test_unparseable_replies_do_not_switch_models():
    garbage = httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}], "usage": {"total_tokens": 5}})
    analyzer, calls = make_analyzer(lambda model: garbage)
    assert await analyzer.analyze(lead()) is None
    assert "tri-fb" not in calls


async def test_a_long_rate_limit_wait_moves_triage_to_the_fallback():
    def behaviour(model):
        if model == "tri":
            return httpx.Response(429, headers={"retry-after": "900"})
        return ok()

    analyzer, calls = make_analyzer(behaviour)
    assert await analyzer.analyze(lead()) is not None
    assert calls == ["tri", "tri-fb"]
