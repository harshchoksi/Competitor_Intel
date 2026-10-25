import json
from datetime import UTC, datetime

import httpx

from src.ads.links import ad_library_links
from src.analysis.metrics import analyze
from src.analysis.types import SiteData
from src.scrapers.base import ProductRecord
from src.social.youtube import YouTubeClient

NOW = datetime(2026, 10, 22, tzinfo=UTC)


def yt_handler(calls):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        q = dict(req.url.params)
        path = req.url.path.rsplit("/", 1)[-1]
        if path == "channels" and "forHandle" in q:
            return httpx.Response(200, json={"items": [{"id": "UC123"}]})
        if path == "channels":
            return httpx.Response(200, json={"items": [{"snippet": {"title": "My Store"}, "statistics": {"subscriberCount": "12500", "videoCount": "80", "viewCount": "900000"},
                                                        "contentDetails": {"relatedPlaylists": {"uploads": "UU123"}}}]})
        if path == "playlistItems":
            items = [{"contentDetails": {"videoId": f"v{i}", "videoPublishedAt": d}} for i, d in enumerate(["2026-10-20T10:00:00Z", "2026-10-05T10:00:00Z", "2026-08-01T10:00:00Z"])]
            return httpx.Response(200, json={"items": items})
        if path == "videos":
            return httpx.Response(200, json={"items": [{"statistics": {"viewCount": "1000", "likeCount": "50", "commentCount": "10"}}, {"statistics": {"viewCount": "3000", "likeCount": "100", "commentCount": "40"}}]})
        return httpx.Response(404)

    return handler


def test_youtube_summary_by_handle():
    calls = []
    out = YouTubeClient("KEY", transport=httpx.MockTransport(yt_handler(calls))).summary("@mystore", now=NOW)
    assert out["status"] == "ok" and out["followers"] == 12500 and out["posts_last_30d"] == 2 and out["avg_views_recent"] == 2000
    assert out["engagement_rate_pct"] == 5.0  # (60 + 140) / 4000
    assert all(r.url.params["key"] == "KEY" for r in calls) and len(calls) == 4


def test_youtube_channel_id_skips_resolution_and_custom_urls_are_unsupported():
    calls = []
    c = YouTubeClient("KEY", transport=httpx.MockTransport(yt_handler(calls)))
    assert c.summary("channel/UC123", now=NOW)["status"] == "ok" and len(calls) == 3
    assert c.summary("c/CustomName", now=NOW)["status"] == "unsupported"


def test_youtube_errors_are_reported_without_leaking_the_key():
    quota = httpx.Response(403, json={"error": {"message": "quota exceeded", "errors": [{"reason": "quotaExceeded"}]}})
    out = YouTubeClient("SECRETKEY", transport=httpx.MockTransport(lambda r: quota)).summary("@x")
    assert out["status"] == "error" and "quotaExceeded" in out["error"] and "SECRETKEY" not in json.dumps(out)

    def boom(req):
        raise httpx.ConnectError("down", request=req)

    out = YouTubeClient("SECRETKEY", transport=httpx.MockTransport(boom)).summary("@x")
    assert out["status"] == "error" and "SECRETKEY" not in json.dumps(out)


def test_hidden_subscriber_count_is_unknown_not_zero():
    def h(req):
        if req.url.path.endswith("channels"):
            return httpx.Response(200, json={"items": [{"snippet": {"title": "T"}, "statistics": {"hiddenSubscriberCount": True, "videoCount": "1"}, "contentDetails": {}}]})
        return httpx.Response(404)

    assert YouTubeClient("K", transport=httpx.MockTransport(h)).summary("channel/UC1")["followers"] is None


def test_ad_library_links():
    l = ad_library_links("shop.example.com", "mystorepage")  # noqa: E741
    assert "country=IN" in l["meta"] and "q=mystorepage" in l["meta"] and l["google"] == "https://adstransparency.google.com/?region=IN&domain=shop.example.com"
    assert ad_library_links("localhost:8102")["search_term"] == "localhost" and "google" not in ad_library_links("localhost")


def test_social_and_ads_in_result_and_insight():
    def site(key, source, yt=None, fb=None):
        return SiteData(key=key, name=f"{key}.test", is_source=source, status="ok", products=[ProductRecord(external_id="1", title="Sonex HP-100 Headphone", brand="Sonex", price=100.0)],
                        signals={"offers": [], "marketing_stack": {}, "social_links": {"facebook": {"handle": fb, "url": "u"}} if fb else {}}, social={"youtube": yt} if yt else None)

    r = analyze([site("me", True), site("rv", False, yt={"status": "ok", "followers": 5000, "posts_last_30d": 3}, fb="rvpage")])
    assert r["social"]["rv"]["youtube"]["followers"] == 5000 and "q=rvpage" in r["ad_links"]["rv"]["meta"] and set(r["ad_links"]) == {"me", "rv"}
    assert any(i["title"] == "YouTube presence" and "5,000" in i["text"] for i in r["insights"])
