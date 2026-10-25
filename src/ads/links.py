"""Links into the free public ad libraries. Meta's official API does not return commercial ads for India, so the
reliable free option is to open the public library pages directly (no scraping, no terms-of-service risk)."""
from __future__ import annotations

import re
from urllib.parse import quote


def _domain_term(name: str) -> str:
    host = re.sub(r"^https?://", "", name).split("/")[0].split(":")[0]
    parts = [p for p in host.split(".") if p and p != "www"]
    return parts[0] if parts else name


def ad_library_links(site_name: str, facebook_handle: str | None = None, country: str = "IN") -> dict:
    term = facebook_handle or _domain_term(site_name)
    links = {"meta": ("https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=" f"{country}&q={quote(term)}&search_type=keyword_unordered&media_type=all"),
             "search_term": term}
    host = re.sub(r"^https?://", "", site_name).split("/")[0]
    if "." in host:
        links["google"] = f"https://adstransparency.google.com/?region={country}&domain={quote(host)}"
    return links
