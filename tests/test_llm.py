import json

import httpx
import pytest

from src.analysis.metrics import analyze
from src.analysis.types import SiteData
from src.llm.gemini import GeminiClient, LLMError, parse_json_object
from src.llm.insights import allowed_numbers, build_llm_input, generate_ai_insights, ungrounded
from src.scrapers.base import ProductRecord


def gemini_reply(obj, status=200, finish="STOP"):
    body = {"candidates": [{"content": {"parts": [{"text": obj if isinstance(obj, str) else json.dumps(obj)}]}, "finishReason": finish}]}
    return httpx.Response(status, json=body)


def client_with(handler, **kw):
    return GeminiClient("secret-key-123", transport=httpx.MockTransport(handler), sleep=lambda s: None, **kw)


def P(title, price, brand="Sonex"):
    return ProductRecord(external_id=title, title=title, brand=brand, price=price, in_stock=True)


@pytest.fixture(scope="module")
def result():
    me = SiteData(key="me", name="me.test", is_source=True, status="ok", products=[P("Sonex HP-100 Headphone", 2000), P("Boomer SP-200 Speaker", 4000, "Boomer")])
    rv = SiteData(key="rv", name="rival.test", is_source=False, status="ok", products=[P("Sonex Headphone HP-100", 1800), P("Boomer SP-200 Speaker", 4200, "Boomer")])
    return analyze([me, rv])


GOOD = {"executive_summary": "rival.test is 10% cheaper on Sonex HP-100 Headphone, while you lead on the Boomer speaker.",
        "key_findings": [{"title": "Headphone price gap", "text": "rival.test sells the HP-100 at ₹1,800 against your ₹2,000.", "severity": "warning"}],
        "per_competitor_notes": {"rival.test": "Cheaper on one of two matched products.", "ghost.test": "invented competitor"},
        "risks": ["Losing headphone sales to rival.test."],
        "recommended_actions": [{"priority": "high", "action": "Review the HP-100 price", "rationale": "The gap is 10%."}]}


def test_gemini_success_and_key_handling():
    seen = {}

    def handler(req):
        seen["url"], seen["key"], seen["body"] = str(req.url), req.headers.get("x-goog-api-key"), json.loads(req.content)
        return gemini_reply({"a": 1})

    assert client_with(handler).generate_json("sys", "prompt") == {"a": 1}
    assert seen["key"] == "secret-key-123" and "secret-key-123" not in seen["url"]
    assert seen["url"].endswith("/models/gemini-2.5-flash:generateContent")
    assert seen["body"]["generationConfig"]["responseMimeType"] == "application/json" and seen["body"]["systemInstruction"]["parts"][0]["text"] == "sys"


def test_gemini_retries_rate_limit_then_succeeds():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, json={"error": {"message": "quota"}}) if len(calls) < 3 else gemini_reply({"ok": True})

    assert client_with(handler).generate_json("s", "p") == {"ok": True} and len(calls) == 3


def test_gemini_gives_up_with_clear_error():
    with pytest.raises(LLMError, match="after 3 attempts: HTTP 503"):
        client_with(lambda r: httpx.Response(503)).generate_json("s", "p")


def test_gemini_400_retries_without_json_mode():
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(400, json={"error": {"message": "bad config"}}) if len(bodies) == 1 else gemini_reply('```json\n{"ok": 1}\n```')

    assert client_with(handler).generate_json("s", "p") == {"ok": 1}
    assert "responseMimeType" in bodies[0]["generationConfig"] and "responseMimeType" not in bodies[1]["generationConfig"]


@pytest.mark.parametrize("response,msg", [
    (httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}), "blocked"),
    (httpx.Response(200, json={"candidates": []}), "no answer"),
    (httpx.Response(401, json={"error": {"message": "API key not valid"}}), "HTTP 401: API key not valid"),
    (gemini_reply("this is not json"), "valid JSON"),
    (gemini_reply('{"cut": "off', finish="MAX_TOKENS"), "cut off"),
])
def test_gemini_failure_modes(response, msg):
    with pytest.raises(LLMError, match=msg):
        client_with(lambda r: response).generate_json("s", "p")


def test_no_key_rejected_and_parse_helper():
    with pytest.raises(LLMError):
        GeminiClient("")
    assert parse_json_object('noise {"a": [1, 2]} trailing') == {"a": [1, 2]}


def test_grounding_rules(result):
    allowed = allowed_numbers(build_llm_input(result))
    assert ungrounded("Gap is 10% and the price is ₹1,800", allowed) == []
    assert ungrounded("They sell it for ₹1,200", allowed) == [1200.0]
    assert ungrounded("Top 3 of 5 products", allowed) == []  # small counts are fine
    assert ungrounded("Rounded to about 8% lower", allowed_numbers({"gap": 7.9})) == []
    assert ungrounded("They are 25% cheaper", allowed_numbers({"gap_pct": -25.0})) == []  # sign-insensitive


def test_llm_input_is_compact_and_has_no_raw_pages(result):
    inp = build_llm_input(result)
    assert len(json.dumps(inp)) < 5000 and inp["biggest_price_threats"][0]["gap_pct"] == -10.0 and "html" not in json.dumps(inp).lower()


def test_ai_insights_keep_grounded_and_drop_invented(result):
    bad = {**GOOD, "key_findings": [*GOOD["key_findings"], {"title": "Made up", "text": "rival.test grew sales by 47% last quarter", "severity": "info"}],
           "risks": [*GOOD["risks"], "Margin falls by ₹9,999 per month"]}
    out = generate_ai_insights(result, client_with(lambda r: gemini_reply(bad)))
    assert out["status"] == "ok" and out["executive_summary"].startswith("rival.test is 10%")
    assert [f["title"] for f in out["key_findings"]] == ["Headphone price gap"]  # the 47% claim was removed
    assert out["risks"] == ["Losing headphone sales to rival.test."] and out["removed_statements"] == 2
    assert list(out["per_competitor_notes"]) == ["rival.test"]  # unknown competitor dropped
    assert out["recommended_actions"][0]["priority"] == "high"


def test_ai_fails_closed_when_nothing_is_grounded(result):
    junk = {"executive_summary": "Sales will rise 83% next year", "key_findings": [{"title": "t", "text": "Revenue is ₹98,765", "severity": "info"}]}
    out = generate_ai_insights(result, client_with(lambda r: gemini_reply(junk)))
    assert out["status"] == "failed" and "not in the data" in out["error"]


@pytest.mark.parametrize("client,use_ai,status", [(None, True, "unavailable"), (None, False, "off")])
def test_ai_status_without_client(result, client, use_ai, status):
    assert generate_ai_insights(result, client, use_ai=use_ai)["status"] == status


def test_ai_malformed_answer_and_network_failure_fall_back(result):
    assert generate_ai_insights(result, client_with(lambda r: gemini_reply({"key_findings": "nope"})))["status"] == "failed"
    assert generate_ai_insights(result, client_with(lambda r: httpx.Response(500)))["status"] == "failed"
