"""YouTube Data API v3 (official, free quota). Public channel statistics and posting cadence only."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import httpx

API = "https://www.googleapis.com/youtube/v3"


class YouTubeError(Exception):
    pass


class YouTubeClient:
    def __init__(self, api_key: str, *, transport: httpx.BaseTransport | None = None, timeout: float = 20.0) -> None:
        self._key = api_key
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, **params) -> dict:
        try:
            r = self._http.get(f"{API}/{path}", params={**params, "key": self._key})
        except httpx.TransportError as exc:
            raise YouTubeError(f"network error ({type(exc).__name__})") from None  # never include the URL: it contains the key
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200:
            err = data.get("error", {})
            reason = (err.get("errors") or [{}])[0].get("reason", "")
            raise YouTubeError(f"HTTP {r.status_code} {reason}: {str(err.get('message', ''))[:120]}".strip())
        return data

    def resolve_channel_id(self, ref: str) -> str | None:
        """ref: '@handle', 'channel/UC…', 'user/name'. Custom '/c/name' URLs cannot be resolved cheaply -> None."""
        ref = ref.strip()
        if ref.startswith("channel/"):
            return ref.split("/", 1)[1]
        if ref.startswith("@"):
            items = self._get("channels", part="id", forHandle=ref).get("items") or []
        elif ref.startswith("user/"):
            items = self._get("channels", part="id", forUsername=ref.split("/", 1)[1]).get("items") or []
        else:
            return None
        return items[0]["id"] if items else None

    def summary(self, ref: str, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        base = {"platform": "youtube", "ref": ref}
        try:
            cid = self.resolve_channel_id(ref)
            if cid is None:
                return {**base, "status": "unsupported", "error": "This channel link format cannot be resolved without extra API quota."}
            items = self._get("channels", part="snippet,statistics,contentDetails", id=cid).get("items") or []
            if not items:
                return {**base, "status": "not_found", "error": "Channel not found."}
            ch = items[0]
            st = ch.get("statistics", {})
            out = {**base, "status": "ok", "title": ch["snippet"]["title"], "url": f"https://www.youtube.com/channel/{cid}",
                   "followers": None if st.get("hiddenSubscriberCount") else _int(st.get("subscriberCount")),
                   "video_count": _int(st.get("videoCount")), "view_count": _int(st.get("viewCount")),
                   "posts_last_30d": None, "avg_views_recent": None, "engagement_rate_pct": None}
            uploads = (ch.get("contentDetails", {}).get("relatedPlaylists", {}) or {}).get("uploads")
            if uploads:
                pl = self._get("playlistItems", part="contentDetails", playlistId=uploads, maxResults=30).get("items") or []
                cutoff = now - timedelta(days=30)
                dates = [datetime.fromisoformat(i["contentDetails"]["videoPublishedAt"].replace("Z", "+00:00")) for i in pl if i.get("contentDetails", {}).get("videoPublishedAt")]
                out["posts_last_30d"] = sum(d >= cutoff for d in dates)
                ids = [i["contentDetails"]["videoId"] for i in pl[:10] if i.get("contentDetails", {}).get("videoId")]
                if ids:
                    vids = self._get("videos", part="statistics", id=",".join(ids)).get("items") or []
                    views = [_int(v["statistics"].get("viewCount")) or 0 for v in vids]
                    inter = [(_int(v["statistics"].get("likeCount")) or 0) + (_int(v["statistics"].get("commentCount")) or 0) for v in vids]
                    if views and sum(views) > 0:
                        out["avg_views_recent"] = round(sum(views) / len(views))
                        out["engagement_rate_pct"] = round(sum(inter) / sum(views) * 100, 2)
            return out
        except YouTubeError as exc:
            return {**base, "status": "error", "error": str(exc)}


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def client_from_env() -> YouTubeClient | None:
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    return YouTubeClient(key) if key else None
