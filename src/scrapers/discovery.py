"""Finding the real online store when the address a user typed is only the company's main website.

Typical case: `lingandsons.com` is the corporate site, the shop is `shop.lingandsons.com`. Reading the corporate homepage finds no
products, so we look for where the store lives: links on the homepage that say "Shop"/"Order online", or the usual store sub-domains."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

SHOP_TEXT = re.compile(r"\b(?:shop|store|e-?shop|web\s?shop|order\s+online|buy\s+online|online\s+(?:shop|store|shopping|grocer\w*|ordering)|shop\s+now|start\s+shopping|grocer(?:y|ies))\b", re.I)
SHOP_HOST = re.compile(r"^(?:shop|store|order|eshop|e-shop|webshop|online|buy|market|tienda|winkel)\.", re.I)
PLATFORM_HOSTS = ("myshopify.com", "mybigcommerce.com", "square.site", "ecwid.com", "bigcartel.com", "shopbase.com", "webshopapp.com", "wixsite.com", "shopify.com")
NOT_STORES = ("facebook.", "instagram.", "twitter.", "x.com", "youtube.", "youtu.be", "tiktok.", "linkedin.", "pinterest.", "google.", "goo.gl", "apple.com", "play.google",
              "wa.me", "whatsapp.", "tripadvisor.", "maps.", "t.me", "snapchat.", "yelp.", "mailto:", "tel:", "wikipedia.", "cloudflare.", "wordpress.com", "gstatic.", "doubleclick.")
GUESS_SUBDOMAINS = ("shop", "store", "order", "eshop", "webshop", "online")
_SECOND_LEVEL = {"co", "com", "org", "net", "gov", "edu", "ac"}


def registrable_domain(host: str) -> str:
    """example.co.uk -> example.co.uk ; shop.example.com -> example.com (good enough without a public-suffix list)."""
    parts = host.lower().split(":")[0].split(".")
    if len(parts) <= 2:
        return ".".join(parts)
    if len(parts[-1]) == 2 and parts[-2] in _SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def find_store_candidates(html: str, base_url: str, limit: int = 4) -> list[str]:
    """Other addresses linked from this page that look like an online store, best first. Returns origins (scheme://host)."""
    base = urlparse(base_url)
    base_domain = registrable_domain(base.netloc)
    soup = BeautifulSoup(html or "", "lxml")
    scores: dict[str, int] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(base_url, href)
        p = urlparse(full)
        if p.scheme not in ("http", "https") or not p.netloc or p.netloc.lower() == base.netloc.lower():
            continue
        host = p.netloc.lower()
        if any(bad in host for bad in NOT_STORES):
            continue
        text = a.get_text(" ", strip=True)
        score = 0
        if registrable_domain(host) == base_domain:
            score += 2
        if SHOP_HOST.match(host):
            score += 3
        if any(host.endswith(ph) for ph in PLATFORM_HOSTS):
            score += 2
        if SHOP_TEXT.search(text) or SHOP_TEXT.search(p.path):
            score += 2
        if score >= 3 or (score >= 2 and registrable_domain(host) == base_domain):
            origin = f"{p.scheme}://{host}"
            scores[origin] = max(scores.get(origin, 0), score)
    return [o for o, _ in sorted(scores.items(), key=lambda kv: -kv[1])][:limit]


def guess_store_origins(host: str, tried: set[str] | None = None, limit: int = 4) -> list[str]:
    """Conventional store sub-domains of the same company (shop.example.com, store.example.com...)."""
    host = host.lower().split(":")[0]
    if SHOP_HOST.match(host):
        return []
    base = registrable_domain(host)
    out = []
    for sub in GUESS_SUBDOMAINS:
        origin = f"https://{sub}.{base}"
        if origin not in (tried or set()):
            out.append(origin)
    return out[:limit]
