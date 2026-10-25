"""Signals read from a site's homepage and policy pages: offers, coupons, shipping, returns, payments,
social links and the marketing/analytics stack. Pure text analysis; nothing here is guessed."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from loguru import logger

from src.scrapers.http import PoliteClient

OFFER_HINTS = re.compile(
    r"\d{1,2}\s*%\s*off|flat\s*(?:₹|rs\.?\s*)?\d+|free\s+(?:shipping|delivery)|use\s+code|coupon|cashback|buy\s*\d+\s*get\s*\d+|\bbogo\b"
    r"|no[- ]cost\s*emi|bank\s+offer|extra\s+\d+\s*%|\bsale\b|festive|diwali|clearance", re.I)
BANNER_ATTR = re.compile(r"announce|promo|banner|offer|topbar|top-bar|marquee|ticker|notice|strip", re.I)
CODE_RE = re.compile(r"(?i:use|apply|code|coupon)\s*(?i:code)?[\s:\-]*[\"'“‘]?([A-Z][A-Z0-9]{3,14})\b")
NOT_CODES = {"CODE", "FREE", "OFFER", "ORDER", "ONLY", "SHIPPING", "DELIVERY", "COUPON", "APPLY", "SALE", "NOW", "ALL", "ORDERS", "CHECKOUT"}
FREE_SHIP_ALL = re.compile(r"free\s+(?:shipping|delivery)\s+(?:on\s+)?(?:all|every|any)\s+orders?", re.I)
FREE_SHIP_MIN = re.compile(r"free\s+(?:shipping|delivery)[^.\n]{0,60}?(?:above|over|orders?\s+of|min(?:imum)?\.?\s*(?:order)?|≥|>=)\s*(?:₹|rs\.?|inr)?\s*([\d,]{3,7})", re.I)
RETURN_DAYS = [re.compile(r"(\d{1,3})[\s-]*days?\s+(?:(?:easy|free|hassle[- ]free|no[- ]questions[- ]asked)\s+)?(?:return|refund|exchange|replacement)", re.I),
               re.compile(r"(?:return|refund|exchange|replacement)[^.\n]{0,80}?(?:within|in)\s*(\d{1,3})\s*days?", re.I)]
WARRANTY = re.compile(r"(\d{1,2})[\s-]*(year|month)s?\s+(?:\w+\s+){0,2}?warranty", re.I)
SHIP_FEE = re.compile(r"(?:shipping|delivery)\s+(?:charges?|fees?|cost)[^.\n]{0,40}?(?:₹|rs\.?)\s*([\d,]{2,6})", re.I)
POLICY_LINK = re.compile(r"return|refund|exchange|shipping|delivery|warranty", re.I)

PAYMENTS = {
    "UPI": r"\bupi\b", "EMI": r"\bemi\b", "No-cost EMI": r"no[- ]cost\s+emi", "Cash on delivery": r"cash on delivery|\bcod\b",
    "Wallets": r"paytm|phonepe|amazon pay|mobikwik", "Cards": r"credit card|debit card|\bvisa\b|mastercard|rupay",
    "Net banking": r"net ?banking", "Pay later": r"\bbnpl\b|simpl|lazypay|zestmoney|pay later",
}
STACK = {
    "advertising": {"Meta Pixel": ("connect.facebook.net/en_us/fbevents.js", "fbq('init'", 'fbq("init"'),
                    "Google Ads tag": ("googleadservices.com", "gtag/js?id=aw-"), "TikTok Pixel": ("analytics.tiktok.com",),
                    "Snapchat Pixel": ("sc-static.net/scevent",), "LinkedIn Insight": ("snap.licdn.com",),
                    "X (Twitter) Pixel": ("static.ads-twitter.com",), "Pinterest Tag": ("s.pinimg.com/ct/core.js",)},
    "analytics": {"Google Analytics 4": ("gtag/js?id=g-",), "Universal Analytics": ("google-analytics.com/analytics.js",),
                  "Google Tag Manager": ("googletagmanager.com/gtm.js",), "Microsoft Clarity": ("clarity.ms",), "Hotjar": ("static.hotjar.com",)},
    "engagement": {"Klaviyo": ("klaviyo.com",), "WebEngage": ("webengage",), "MoEngage": ("moengage",), "CleverTap": ("clevertap",)},
    "reviews": {"Judge.me": ("judge.me",), "Yotpo": ("yotpo",), "Stamped": ("stamped.io",), "Okendo": ("okendo.io",)},
    "checkout": {"Razorpay": ("razorpay",), "PayU": ("payu",), "Cashfree": ("cashfree",), "GoKwik": ("gokwik",), "Shiprocket": ("shiprocket",)},
    "support": {"Intercom": ("intercom",), "Tawk.to": ("tawk.to",), "Zendesk": ("zendesk",), "Freshchat": ("freshchat",),
                "WhatsApp chat": ("wa.me/", "api.whatsapp.com")},
}
SOCIAL = [
    ("instagram", re.compile(r"instagram\.com/([A-Za-z0-9_.]+)", re.I), {"p", "reel", "reels", "explore", "accounts", "stories", "tv", "share"}),
    ("facebook", re.compile(r"facebook\.com/([A-Za-z0-9_.\-]+)", re.I), {"sharer", "sharer.php", "share", "tr", "plugins", "dialog", "login", "pages", "groups", "policies", "help", "share.php"}),
    ("youtube", re.compile(r"youtube\.com/((?:@|channel/|c/|user/)[A-Za-z0-9_.\-%]+)", re.I), set()),
    ("twitter", re.compile(r"(?:twitter|x)\.com/([A-Za-z0-9_]+)", re.I), {"intent", "share", "home", "i", "search"}),
    ("linkedin", re.compile(r"linkedin\.com/company/([A-Za-z0-9_.\-]+)", re.I), set()),
    ("tiktok", re.compile(r"tiktok\.com/(@[A-Za-z0-9_.]+)", re.I), set()),
    ("pinterest", re.compile(r"pinterest\.[a-z.]+/([A-Za-z0-9_.\-]+)", re.I), {"pin", "pin-builder"}),
]


def _find_code(text: str) -> str | None:
    for m in CODE_RE.finditer(text):
        if m.group(1) not in NOT_CODES:
            return m.group(1)
    return None


def classify_offer(text: str) -> dict:
    low = text.lower()
    code = _find_code(text)
    pct = [int(x) for x in re.findall(r"(\d{1,2})\s*%\s*off", low)]
    if code:
        kind = "coupon"
    elif re.search(r"free\s+(shipping|delivery)", low):
        kind = "free_shipping"
    elif re.search(r"\bbank\b|credit card|debit card|hdfc|icici|\bsbi\b|\baxis\b", low):
        kind = "bank_offer"
    elif "emi" in low:
        kind = "emi"
    elif re.search(r"buy\s*\d+\s*get\s*\d+|\bbogo\b|combo|bundle", low):
        kind = "bundle"
    elif pct or re.search(r"flat|extra\s+\d+", low):
        kind = "discount"
    else:
        kind = "promo"
    return {"type": kind, "text": text[:160], "code": code, "discount_value": float(max(pct)) if pct else None}


def detect_stack(raw_html: str) -> dict[str, list[str]]:
    low = raw_html.lower()
    out: dict[str, list[str]] = {}
    for category, tools in STACK.items():
        found = [name for name, needles in tools.items() if any(n in low for n in needles)]
        if category == "advertising" and re.search(r"aw-\d{6,}", low) and "Google Ads tag" not in found:
            found.append("Google Ads tag")
        if found:
            out[category] = found
    return out


def find_social(soup: BeautifulSoup) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        for platform, rx, skip in SOCIAL:
            if platform in found:
                continue
            m = rx.search(href)
            if m and m.group(1).lower().rstrip("/") not in skip:
                handle = m.group(1)
                found[platform] = {"handle": handle, "url": href if href.startswith("http") else "https://" + href.lstrip("/")}
    return found


def _clean_text(soup: BeautifulSoup) -> BeautifulSoup:
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    return soup


def extract_offers(soup: BeautifulSoup) -> list[dict]:
    texts: list[str] = []
    for el in [*soup.find_all(class_=BANNER_ATTR), *soup.find_all(id=BANNER_ATTR)]:
        t = el.get_text(" ", strip=True)
        if 8 <= len(t) <= 200 and OFFER_HINTS.search(t):
            texts.append(t)
    texts += [s for s in soup.stripped_strings if 8 <= len(s) <= 160 and OFFER_HINTS.search(s)]
    seen: set[str] = set()
    offers = []
    for t in texts:
        key = re.sub(r"\W+", " ", t.lower()).strip()
        if key in seen or any(key in s for s in seen):
            continue
        seen.add(key)
        offers.append(classify_offer(re.sub(r"\s+", " ", t)))
        if len(offers) >= 12:
            break
    return offers


def parse_shipping(text: str) -> tuple[int | None, bool]:
    if FREE_SHIP_ALL.search(text):
        return 0, True
    m = FREE_SHIP_MIN.search(text)
    if m:
        try:
            return int(m.group(1).replace(",", "")), False
        except ValueError:
            pass
    return None, False


def extract_policies(text: str) -> dict:
    out: dict = {"return_window_days": None, "warranty": None, "shipping_fee": None}
    for rx in RETURN_DAYS:
        m = rx.search(text)
        if m:
            out["return_window_days"] = int(m.group(1))
            break
    m = WARRANTY.search(text)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        out["warranty"] = f"{n} {unit}{'s' if n != 1 else ''}"
    m = SHIP_FEE.search(text)
    if m:
        out["shipping_fee"] = int(m.group(1).replace(",", ""))
    return out


def extract_signals(html: str, base_url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    stack = detect_stack(html)
    social = find_social(soup)
    host = urlparse(base_url).netloc
    links = []
    for a in soup.find_all("a", href=True):
        full = urljoin(base_url.rstrip("/") + "/", a["href"]).split("#")[0]
        if urlparse(full).netloc == host and (POLICY_LINK.search(a.get_text(" ", strip=True)) or POLICY_LINK.search(urlparse(full).path)):
            if not any(x in full.lower() for x in ("/products/", "/collections/", "/cart", "/account")) and full not in links:
                links.append(full)
    links.sort(key=lambda u: 0 if re.search(r"polic|return|refund|shipping|warranty", u, re.I) else 1)
    _clean_text(soup)
    text = soup.get_text(" ", strip=True)
    threshold, free_all = parse_shipping(text)
    low = text.lower()
    return {"offers": extract_offers(soup), "free_shipping_threshold": threshold, "free_shipping_all": free_all,
            "payment_methods": [k for k, rx in PAYMENTS.items() if re.search(rx, low)], "social_links": social,
            "marketing_stack": stack, "policy_links": links[:4], "_text": text}


def collect_site_signals(client: PoliteClient, origin: str, max_policy_pages: int = 3) -> dict | None:
    """Fetch the homepage (usually cached from detection) and a few policy pages. Never raises."""
    try:
        home = client.get(origin)
        if home is None or not home.ok:
            return None
        sig = extract_signals(home.text, origin)
        texts = [sig.pop("_text")]
        pages = []
        for url in sig["policy_links"][:max_policy_pages]:
            res = client.get(url)
            if res is not None and res.ok:
                pages.append(url)
                texts.append(_clean_text(BeautifulSoup(res.text, "lxml")).get_text(" ", strip=True)[:200_000])
        policies = extract_policies(" ".join(texts))
        if sig["free_shipping_threshold"] is None and not sig["free_shipping_all"]:
            sig["free_shipping_threshold"], sig["free_shipping_all"] = parse_shipping(" ".join(texts))
        sig["policies"] = policies
        sig["policy_pages"] = pages
        return sig
    except Exception:
        logger.exception("signals collection failed for {}", origin)
        return None
