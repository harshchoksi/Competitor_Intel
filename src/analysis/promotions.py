"""Competitor promotion intelligence from what each website shows publicly.

Two complementary sources, both read from the sites themselves:

  1. PRODUCT DISCOUNTS   every product with a crossed-out "was" price. Gives discount AMOUNT (%, money), on WHICH PRODUCT TYPES,
                         and how much of the catalogue is on sale. Measured, not guessed.
  2. CAMPAIGN MESSAGES   banner / announcement-bar / pop-up / hero text such as "Flat 20% off all meat, ends 15 Nov, use code FRESH20".
                         Parsed into amount, what it applies to, conditions, code and the PERIOD (start/end dates).

The period can also be observed over time: see `attach_promo_history` (first time a discount was seen, how long it has run).

The text parser is deterministic (regexes + the product-type taxonomy) so every figure traces back to words on the page.
The same `parse_promotion` function is what social-media captions will be fed through in a later phase."""
from __future__ import annotations

import re
import statistics
from datetime import date, datetime, timedelta

from src.analysis.money import fmt
from src.analysis.taxonomy import categorize_text, infer_product_type
from src.analysis.types import SiteData

# --------------------------------------------------------------------------- text parsing
_CUR = r"(?:₹|rs\.?|inr|us\$|\$|usd|€|eur|£|gbp|afl\.?|awg|aed|a\$|c\$)"
_NUM = r"(\d[\d,]*(?:\.\d+)?)"
_PCT_OFF = re.compile(r"(\d{1,2}(?:\.\d+)?)\s*%\s*(?:off|discount|cash\s?back|savings?|less)", re.I)
_PCT_LEAD = re.compile(r"(?:save|flat|extra|additional|upto|up\s*to|get|min\.?|minimum|discounts?\s+of)\s*(?:up\s*to\s*)?(\d{1,2}(?:\.\d+)?)\s*%", re.I)
_UP_TO = re.compile(r"\bup\s*to\b|\bupto\b|\bas\s+much\s+as\b|\bstarting\s+(?:from|at)\b", re.I)
_FLAT_OFF = re.compile(rf"(?:flat\s*|save\s*|get\s*)?{_CUR}\s*{_NUM}\s*(?:off|discount)|(?:off|discount)\s+(?:of\s+)?{_CUR}\s*{_NUM}|(?:save|flat)\s*{_CUR}\s*{_NUM}", re.I)
_BOGO = re.compile(r"buy\s*(\d+)\s*,?\s*get\s*(\d+)(?:\s*free)?|\bbogo(?:f)?\b|\b(\d+)\s*for\s*(?:the\s*price\s*of\s*)?(\d+)\b(?!\d|%|\s*(?:days?|hours?|hrs?))", re.I)
_FREE_SHIP = re.compile(r"free\s+(?:shipping|delivery)", re.I)
_MIN_SPEND = re.compile(rf"(?:orders?\s+(?:of|above|over|worth)|(?:above|over)|min(?:imum)?\.?\s*(?:purchase|order|spend|cart|basket)?\s*(?:of|:)?)\s*{_CUR}?\s*{_NUM}"
                        r"(?!\s*(?:%|(?:st|nd|rd|th)\b|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|days?|hours?|hrs?|kg|g\b|ml|pcs|items?))", re.I)
_SITEWIDE = re.compile(r"site[- ]?wide|store[- ]?wide|everything|entire\s+(?:store|site|range|collection)|all\s+(?:products|items|orders|categories|groceries)|on\s+all\b|sitewide", re.I)
_SELECTED = re.compile(r"\bselected\b|\bselect\s+(?:items|products)|\bchosen\b|\bsome\s+items\b", re.I)
_CONDITION = [
    ("first order", re.compile(r"first\s+(?:order|purchase)|new\s+(?:customers?|users?)|sign[- ]?up|subscribe|newsletter", re.I)),
    ("app only", re.compile(r"\bapp\b(?:\s+only|\s+exclusive)?|download\s+our", re.I)),
    ("members only", re.compile(r"members?\s+only|loyalty|club\s+members?", re.I)),
    ("online only", re.compile(r"online\s+only|web\s+exclusive", re.I)),
    ("while stocks last", re.compile(r"while\s+(?:stocks?|supplies)\s+last|limited\s+stock", re.I)),
]
_EVENTS = re.compile(r"black\s+friday|cyber\s+monday|diwali|dussehra|christmas|boxing\s+day|new\s+year|easter|eid|ramadan|valentine|mother'?s\s+day|father'?s\s+day|independence\s+day|"
                     r"republic\s+day|holi|onam|pongal|back\s+to\s+school|summer\s+sale|winter\s+sale|end[- ]of[- ]season|clearance|flash\s+sale|mega\s+sale|weekend\s+sale|anniversary|carnival|"
                     r"dia\s+di\s+sol|queen'?s\s+day|king'?s\s+day|fiesta", re.I)
_CODE = re.compile(r"(?i:use|apply|enter|code|coupon|promo)\s*(?i:code|coupon)?[\s:\-]*[\"'“‘]?([A-Z][A-Z0-9]{3,14})\b")
_NOT_CODES = {"CODE", "FREE", "OFFER", "ORDER", "ONLY", "SHIPPING", "DELIVERY", "COUPON", "APPLY", "SALE", "NOW", "ALL", "ORDERS", "CHECKOUT", "OFF", "TODAY", "WEEKEND"}
# words that make a sentence a promotion at all
PROMO_HINT = re.compile(r"\d{1,2}\s*%|%\s*off|\boff\b|discount|\bsale\b|\bdeal|offer|save\s|savings|free\s+(?:shipping|delivery|gift)|buy\s*\d+\s*get|\bbogo\b|coupon|promo|cash\s?back|"
                        r"clearance|bargain|special\s+price|price\s+drop|flash|festive|black\s+friday|use\s+code", re.I)

_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
_RX_DMY = re.compile(rf"(?<![\d/])\b{_DAY}\s*(?:of\s+)?{_MON}\b\.?(?:,?\s*(\d{{4}}))?", re.I)
_RX_MDY = re.compile(rf"\b{_MON}\b\.?\s*{_DAY}(?!\d)(?:,?\s*(\d{{4}}))?", re.I)
_RX_NUM = re.compile(r"(?<![\d.])(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?![\d.]*\s*%)(?!\d)|(?<![\d.])(\d{1,2})-(\d{1,2})-(\d{4})(?!\d)")
_RX_SAME_MONTH = re.compile(rf"{_DAY}\s*(?:-|–|—|to|till|until|through|thru)\s*{_DAY}\s*(?:of\s+)?{_MON}\b\.?(?:,?\s*(\d{{4}}))?", re.I)
_END_WORDS = re.compile(r"(?:till|until|til|through|thru|ends?|ending|end\s+on|valid\s+(?:till|until|through|upto|up\s*to)|expires?(?:\s+on)?|last\s+day|by|upto|up\s*to|before)\s*[:\-]?\s*(?:on\s+)?$", re.I)
_START_WORDS = re.compile(r"(?:from|starts?|starting|begins?|beginning|since|valid\s+from|launching)\s*[:\-]?\s*(?:on\s+)?$", re.I)
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _resolve(day: int, month: int, year: int | None, today: date, *, after: date | None = None) -> date | None:
    try:
        if year is not None:
            y = year + 2000 if year < 100 else year
            return date(y, month, day)
        d = date(today.year, month, day)
        if after is not None:
            return d if d >= after else date(today.year + 1, month, day)
        return d if d >= today - timedelta(days=60) else date(today.year + 1, month, day)  # long past -> assume next year
    except ValueError:
        return None


def _month_no(name: str) -> int:
    return _MONTHS[name[:3].lower()]


def _dates_in(text: str, today: date) -> list[tuple[int, int, date]]:
    found: list[tuple[int, int, date]] = []
    taken: list[tuple[int, int]] = []

    def free(s: int, e: int) -> bool:
        return not any(s < te and e > ts for ts, te in taken)

    for m in _RX_SAME_MONTH.finditer(text):  # "1st-15th Nov": reserve the span (parse_period reads it as a range)
        taken.append((m.start(), m.end()))
    for m in _RX_DMY.finditer(text):
        if free(m.start(), m.end()):
            d = _resolve(int(m.group(1)), _month_no(m.group(2)), int(m.group(3)) if m.group(3) else None, today)
            if d:
                found.append((m.start(), m.end(), d))
                taken.append((m.start(), m.end()))
    for m in _RX_MDY.finditer(text):
        if free(m.start(), m.end()):
            d = _resolve(int(m.group(2)), _month_no(m.group(1)), int(m.group(3)) if m.group(3) else None, today)
            if d:
                found.append((m.start(), m.end(), d))
                taken.append((m.start(), m.end()))
    for m in _RX_NUM.finditer(text):
        if free(m.start(), m.end()):
            g = m.groups()
            a, b, yr = (int(g[0]), int(g[1]), g[2]) if g[0] else (int(g[3]), int(g[4]), g[5])
            day, mon = (a, b) if (a > 12 or b <= 12) else (b, a)  # day/month order (India, Aruba, Europe)
            if 1 <= mon <= 12:
                d = _resolve(day, mon, int(yr) if yr else None, today)
                if d:
                    found.append((m.start(), m.end(), d))
    return sorted(found)


def parse_period(text: str, today: date) -> dict:
    """Start/end of the offer as stated in the text. Returns {start, end, dates_stated, dates_inferred, duration_days, event}."""
    low = text.lower()
    out: dict = {"start": None, "end": None, "dates_stated": False, "dates_inferred": False, "duration_days": None}
    dates = _dates_in(text, today)
    rng = _RX_SAME_MONTH.search(text)
    if rng:
        mon, yr = _month_no(rng.group(3)), int(rng.group(4)) if rng.group(4) else None
        a = _resolve(int(rng.group(1)), mon, yr, today)
        b = _resolve(int(rng.group(2)), mon, yr, today, after=a) if a else None
        if a and b:
            out.update(start=a.isoformat(), end=b.isoformat(), dates_stated=True)
    if not out["dates_stated"] and len(dates) >= 2:
        between = text[dates[0][1]:dates[1][0]]
        if re.fullmatch(r"\s*(?:to|till|until|through|thru|and|-|–|—|&)\s*", between, re.I) or _START_WORDS.search(text[max(0, dates[0][0] - 20):dates[0][0]]):
            start, end = dates[0][2], dates[1][2]
            if end < start:
                end = date(end.year + 1, end.month, end.day)
            out.update(start=start.isoformat(), end=end.isoformat(), dates_stated=True)
    if not out["dates_stated"] and dates:
        s, e, d = dates[-1] if _END_WORDS.search(text[max(0, dates[-1][0] - 28):dates[-1][0]]) else dates[0]
        before = text[max(0, s - 28):s]
        if _END_WORDS.search(before):
            out.update(end=d.isoformat(), dates_stated=True)
        elif _START_WORDS.search(before):
            out.update(start=d.isoformat(), dates_stated=True)
        elif re.search(r"\bon\s*$", before, re.I) or re.match(r"\s*(?:only|event)\b", text[e:], re.I):
            out.update(start=d.isoformat(), end=d.isoformat(), dates_stated=True)  # a one-day event
    if not out["dates_stated"]:
        if re.search(r"today\s+only|ends?\s+today|last\s+day\s+today|\btonight\b|ends?\s+at\s+midnight|only\s+today", low):
            out.update(end=today.isoformat(), dates_inferred=True)
        elif re.search(r"this\s+weekend|weekend\s+(?:only|sale|special|deal)", low):
            out.update(end=(today + timedelta(days=(6 - today.weekday()) % 7)).isoformat(), dates_inferred=True)
        elif re.search(r"this\s+week\b|week(?:ly)?\s+(?:special|deal)s?", low):
            out.update(end=(today + timedelta(days=(6 - today.weekday()) % 7)).isoformat(), dates_inferred=True)
        else:
            m = re.search(r"(?:ends?|till|until|through|valid\s+(?:till|until))\s+(?:on\s+)?(" + "|".join(_WEEKDAYS) + ")", low)
            if m:
                out.update(end=(today + timedelta(days=(_WEEKDAYS.index(m.group(1)) - today.weekday()) % 7)).isoformat(), dates_inferred=True)
    m = re.search(r"(\d{1,3})[\s-]*days?\s*(?:only|sale|offer|special|deal)|for\s+(\d{1,3})\s+days", low)
    if m:
        out["duration_days"] = int(m.group(1) or m.group(2))
    ev = _EVENTS.search(text)
    out["event"] = re.sub(r"\s+", " ", ev.group(0)).title() if ev else None
    return out


_PROMO_CATS = [
    (re.compile(r"\bmeats?\b|chicken|beef|pork|lamb|poultry|mutton", re.I), "Meat & Poultry"),
    (re.compile(r"sea\s?food|\bfish\b|prawn|shrimp|salmon", re.I), "Seafood"),
    (re.compile(r"\b(?:dairy|milk|cheese|yog[h]?urt|butter|eggs?)\b", re.I), "Dairy & Eggs"),
    (re.compile(r"fruits?|vegetables?|veggies|produce|fresh\s+(?:fruit|veg)", re.I), "Fruits & Vegetables"),
    (re.compile(r"\b(?:bakery|bread|cakes?|pastr(?:y|ies))\b", re.I), "Bakery"),
    (re.compile(r"beverages?|drinks?|juices?|soda|soft\s+drinks?|water\b", re.I), "Juices & Soft Drinks"),
    (re.compile(r"\bwine|beer|liquor|spirits|alcohol", re.I), "Alcohol"),
    (re.compile(r"snacks?|chips|chocolates?|sweets|candy|cookies|biscuits", re.I), "Snacks & Sweets"),
    (re.compile(r"frozen", re.I), "Frozen Foods"),
    (re.compile(r"\b(?:pantry|staples|rice|pasta|oil|flour|canned)\b", re.I), "Pantry & Staples"),
    (re.compile(r"coffee|\btea\b", re.I), "Coffee & Tea"),
    (re.compile(r"baby|diapers?|nappies", re.I), "Baby"),
    (re.compile(r"cleaning|household|laundry|detergent", re.I), "Household & Cleaning"),
    (re.compile(r"personal\s+care|beauty|skincare|cosmetics", re.I), "Personal Care"),
    (re.compile(r"headphones?|earbuds?|earphones?", re.I), "Headphones & Earbuds"),
    (re.compile(r"\b(?:electronics|gadgets|smartphones?|phones?|laptops?)\b", re.I), "Electronics"),
    (re.compile(r"shoes|footwear|sneakers", re.I), "Footwear"),
    (re.compile(r"clothing|apparel|fashion|t-?shirts?|dresses|jeans", re.I), "Clothing & Fashion"),
]


def _applies_to(text: str) -> tuple[str | None, str | None]:
    """-> (label, product_type or None). 'Everything' for sitewide offers, otherwise the product type named in the text."""
    for rx, cat in _PROMO_CATS:
        if rx.search(text):
            return cat, cat
    hit = categorize_text(text)
    if hit:
        return hit, hit
    if _SITEWIDE.search(text):
        return "Everything", None
    if _SELECTED.search(text):
        return "Selected items", None
    return None, None


def _to_float(s: str) -> float:
    return float(s.replace(",", ""))


def parse_promotion(text: str, today: date, currency: str = "INR") -> dict | None:
    """One banner/caption sentence -> structured promotion, or None when it is not a promotion."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) < 6 or not PROMO_HINT.search(text):
        return None
    pcts = [_to_float(m.group(1)) for m in _PCT_OFF.finditer(text)] + [_to_float(m.group(1)) for m in _PCT_LEAD.finditer(text)]
    pcts = [p for p in pcts if 0 < p <= 95]
    flats = [_to_float(g) for m in _FLAT_OFF.finditer(text) for g in m.groups() if g]
    flats = [f for f in flats if f > 0]
    bogo = _BOGO.search(text)
    code = next((c for c in _CODE.findall(text) if c not in _NOT_CODES), None)
    kind, amount, unit, effective = "sale", None, None, None
    if pcts:
        kind, amount, unit, effective = "percent_off", max(pcts), "%", max(pcts)
    elif flats:
        kind, amount, unit = "flat_off", max(flats), "currency"
    elif bogo:
        kind = "bogo"
        if bogo.group(1):
            buy, get = int(bogo.group(1)), int(bogo.group(2))
            effective = round(get / (buy + get) * 100, 1) if buy + get else None
            amount, unit = effective, "%"
        elif bogo.group(3) and int(bogo.group(3)) > int(bogo.group(4)) > 0:  # "2 for 1"
            effective = round((int(bogo.group(3)) - int(bogo.group(4))) / int(bogo.group(3)) * 100, 1)
            amount, unit = effective, "%"
        elif re.search(r"\bbogo", text, re.I):
            effective = amount = 50.0
            unit = "%"
    elif _FREE_SHIP.search(text):
        kind = "free_shipping"
    elif code:
        kind = "coupon"
    elif not re.search(r"\bsale\b|deal|offer|clearance|flash|bargain|special\s+price|price\s+drop|black\s+friday|festive", text, re.I):
        return None
    label, ptype = _applies_to(text)
    ms = _MIN_SPEND.search(text) if re.search(r"orders?\s+(?:of|above|over)|min|above|over", text, re.I) else None
    min_spend = None
    if ms and kind in ("free_shipping", "percent_off", "flat_off", "coupon"):
        try:
            v = _to_float(ms.group(1))
            min_spend = v if v >= 10 and not re.search(r"\d\s*%", text[max(0, ms.start()):ms.end() + 2]) else None
        except ValueError:
            pass
    period = parse_period(text, today)
    out = {
        "kind": kind, "amount": amount, "unit": unit, "up_to": bool(_UP_TO.search(text)) if amount else False, "effective_pct": effective,
        "applies_to": label, "product_type": ptype, "min_spend": min_spend, "code": code, "currency": currency,
        "conditions": [name for name, rx in _CONDITION if rx.search(text)], "text": text[:300],
    }
    apply_period(out, period, today)
    return out


def apply_period(p: dict, period: dict, today: date) -> None:
    """Fill start/end/status/days_left on a promotion from a parsed period."""
    p.update(start=period["start"], end=period["end"], dates_stated=period["dates_stated"], dates_inferred=period["dates_inferred"],
             duration_days=period["duration_days"], event=period["event"])
    status, days_left = "ongoing", None
    start = date.fromisoformat(period["start"]) if period["start"] else None
    end = date.fromisoformat(period["end"]) if period["end"] else None
    if end is not None:
        days_left = (end - today).days
        status = "expired" if end < today else "active"
    if start is not None and start > today:
        status, days_left = "upcoming", (start - today).days
    elif start is not None and end is None:
        status = "active"
    p.update(status=status, days_left=days_left)


def describe_promotion(p: dict, currency: str) -> str:
    """'20% off Meat & Poultry'-style summary."""
    if p["kind"] == "percent_off":
        what = f"{'Up to ' if p['up_to'] else ''}{p['amount']:g}% off"
    elif p["kind"] == "flat_off":
        what = f"{'Up to ' if p['up_to'] else ''}{fmt(p['amount'], currency)} off"
    elif p["kind"] == "bogo":
        what = "Buy one get one" if p.get("effective_pct") == 50 else "Multi-buy offer"
    elif p["kind"] == "free_shipping":
        what = "Free shipping" + (f" over {fmt(p['min_spend'], currency)}" if p.get("min_spend") else "")
    elif p["kind"] == "coupon":
        what = "Coupon offer"
    else:
        what = "Sale"
    return what + (f" on {p['applies_to']}" if p.get("applies_to") and p["applies_to"] != "Everything" else (" sitewide" if p.get("applies_to") == "Everything" else ""))


# --------------------------------------------------------------------------- product discounts
BANDS = [("under 10%", 0, 10), ("10-20%", 10, 20), ("20-30%", 20, 30), ("30-50%", 30, 50), ("50%+", 50, 101)]


def _pct(a: float, b: float) -> float:
    return a / b * 100 if b else 0.0


def site_discounts(sd: SiteData, currency: str) -> dict:
    """Discount depth measured from products showing a crossed-out original price."""
    n_all = len(sd.products)
    with_was = sale = 0
    pcts: list[float] = []
    savings: list[float] = []
    by_type: dict[str, dict] = {}
    deals: list[dict] = []
    for p in sd.products:
        ptype = infer_product_type(p)
        t = by_type.setdefault(ptype, {"type": ptype, "products": 0, "pcts": [], "savings": []})
        t["products"] += 1
        if p.compare_at_price:
            with_was += 1
        if not (p.price and p.compare_at_price and p.compare_at_price > p.price * 1.005):
            continue
        pct = (p.compare_at_price - p.price) / p.compare_at_price * 100
        if pct > 95:  # a "was" price this much higher is almost certainly a data error
            continue
        sale += 1
        pcts.append(pct)
        savings.append(p.compare_at_price - p.price)
        t["pcts"].append(pct)
        t["savings"].append(p.compare_at_price - p.price)
        deals.append({"title": p.title, "price": round(p.price, 2), "was": round(p.compare_at_price, 2), "pct": round(pct, 1), "saving": round(p.compare_at_price - p.price, 2),
                      "type": ptype, "url": p.url, "image": p.image_url, "in_stock": p.in_stock})
    types = []
    for t in by_type.values():
        pc = t["pcts"]
        types.append({"type": t["type"], "products": t["products"], "on_sale": len(pc), "on_sale_pct": round(_pct(len(pc), t["products"]), 1),
                      "avg_discount_pct": round(statistics.fmean(pc), 1) if pc else None, "median_discount_pct": round(statistics.median(pc), 1) if pc else None,
                      "max_discount_pct": round(max(pc), 1) if pc else None, "avg_saving": round(statistics.fmean(t["savings"]), 2) if pc else None})
    types.sort(key=lambda r: (-r["on_sale"], -r["products"]))
    deals.sort(key=lambda d: -d["pct"])
    return {
        "products": n_all, "publishes_original_price": with_was > 0, "on_sale": sale, "on_sale_pct": round(_pct(sale, n_all), 1),
        "avg_discount_pct": round(statistics.fmean(pcts), 1) if pcts else None, "median_discount_pct": round(statistics.median(pcts), 1) if pcts else None,
        "max_discount_pct": round(max(pcts), 1) if pcts else None, "avg_saving": round(statistics.fmean(savings), 2) if savings else None,
        "bands": [{"label": lab, "count": sum(1 for x in pcts if lo <= x < hi)} for lab, lo, hi in BANDS],
        "by_type": types, "top_deals": deals[:10],
    }


# --------------------------------------------------------------------------- campaigns
_SEGMENT_SPLIT = re.compile(r"\s*(?:\||•|·|‖)\s*")


def parse_banner(text: str, today: date, currency: str) -> list[dict]:
    """One banner may hold several offers ("20% off meat | Extra 10% with code X, ends 17 Nov"). Split them, parse each, and let a
    date-only segment ("Valid till 15 Nov") apply to the offers in the same banner that have no dates of their own."""
    segments = [t for t in _SEGMENT_SPLIT.split(re.sub(r"\s+", " ", text or "")) if t.strip()] or [text]
    promos: list[dict] = []
    date_only: dict | None = None
    for seg in segments:
        p = parse_promotion(seg, today, currency)
        if p is not None:
            promos.append(p)
        else:
            per = parse_period(seg, today)
            if (per["dates_stated"] or per["dates_inferred"]) and date_only is None:
                date_only = per
    if len(segments) > 1:  # also try the whole text, for offers whose words and dates were split by a separator
        whole = parse_promotion(text, today, currency)
        if whole is not None and not promos:
            promos.append(whole)
    if date_only:
        for p in promos:
            if not p["dates_stated"] and not p["dates_inferred"]:
                apply_period(p, date_only, today)
    return promos


def site_campaigns(sd: SiteData, today: date, currency: str) -> list[dict]:
    """Structured promotions from banner/offer text found on the homepage."""
    sig = sd.signals or {}
    texts: list[tuple[str, str]] = [(t, "banner") for t in sig.get("announcements", [])] + [(o.get("text", ""), "offer") for o in sig.get("offers", [])]
    out: list[dict] = []
    seen: dict[tuple, int] = {}
    for text, source in texts:
        for p in parse_banner(text, today, currency):
            key = (p["kind"], p["amount"], p["applies_to"], p["end"], p["code"])
            if key in seen:
                if len(p["text"]) > len(out[seen[key]]["text"]):
                    out[seen[key]]["text"] = p["text"]
                continue
            # a short fragment already contained in a longer message ("20% off") is the same promotion
            low = re.sub(r"\W+", " ", p["text"].lower()).strip()
            if any(low in re.sub(r"\W+", " ", o["text"].lower()) for o in out if len(low) < len(o["text"])):
                continue
            p["source"] = source
            p["summary"] = describe_promotion(p, currency)
            seen[key] = len(out)
            out.append(p)
    order = {"active": 0, "upcoming": 1, "ongoing": 2, "expired": 3}
    out.sort(key=lambda p: (order.get(p["status"], 4), -(p["amount"] or 0)))
    return out[:25]


# --------------------------------------------------------------------------- everything together
def build_promotions(sites: list[SiteData], source_key: str, currency: str, today: date | None = None) -> dict:
    today = today or datetime.now().date()
    ok = [s for s in sites if s.status == "ok"]
    per_site: dict[str, dict] = {}
    campaigns: list[dict] = []
    for sd in ok:
        d = site_discounts(sd, currency)
        d.update(name=sd.name, is_source=sd.is_source)
        per_site[sd.key] = d
        for c in site_campaigns(sd, today, currency):
            campaigns.append({**c, "site_key": sd.key, "site_name": sd.name, "is_source": sd.is_source})
    matrix = _type_matrix(per_site, source_key)
    out = {
        "today": today.isoformat(), "currency": currency, "source_key": source_key, "sites": per_site, "campaigns": campaigns, "types": matrix, "history": {},
        "summary": _summary(per_site, campaigns, source_key),
    }
    out["insights"], out["recommendations"] = _insights(out, source_key, currency)
    return out


def _type_matrix(per_site: dict[str, dict], source_key: str) -> list[dict]:
    types: dict[str, dict] = {}
    for key, d in per_site.items():
        for t in d["by_type"]:
            if t["on_sale"] or key == source_key:
                types.setdefault(t["type"], {})[key] = {k: t[k] for k in ("products", "on_sale", "on_sale_pct", "avg_discount_pct", "max_discount_pct")}
    rows = []
    for ptype, cells in types.items():
        mine = cells.get(source_key)
        comp = {k: v for k, v in cells.items() if k != source_key and v["on_sale"]}
        if not cells or (not comp and not (mine and mine["on_sale"])):
            continue
        best = max(comp.items(), key=lambda kv: kv[1]["avg_discount_pct"] or 0) if comp else None
        yours = mine["avg_discount_pct"] if mine and mine["on_sale"] else None
        gap = round((best[1]["avg_discount_pct"] or 0) - (yours or 0), 1) if best else None
        rows.append({"type": ptype, "sites": cells, "yours": yours, "yours_on_sale": mine["on_sale"] if mine else 0, "best_competitor": best[0] if best else None,
                     "best_competitor_avg": best[1]["avg_discount_pct"] if best else None, "competitors_on_sale": sum(v["on_sale"] for v in comp.values()), "gap_pts": gap})
    rows.sort(key=lambda r: (-r["competitors_on_sale"], -(r["gap_pts"] or 0)))
    return rows


def _summary(per_site: dict[str, dict], campaigns: list[dict], source_key: str) -> dict:
    comps = [(k, d) for k, d in per_site.items() if k != source_key]
    measured = [(k, d) for k, d in comps if d["on_sale"]]
    deepest = max(measured, key=lambda kv: kv[1]["avg_discount_pct"] or 0, default=None)
    broadest = max(measured, key=lambda kv: kv[1]["on_sale_pct"], default=None)
    active = [c for c in campaigns if not c["is_source"] and c["status"] in ("active", "ongoing", "upcoming")]
    return {
        "competitors_with_sales": len(measured), "competitors": len(comps), "no_original_price": [d["name"] for k, d in comps if not d["publishes_original_price"]],
        "deepest": {"key": deepest[0], "name": deepest[1]["name"], "avg_discount_pct": deepest[1]["avg_discount_pct"]} if deepest else None,
        "broadest": {"key": broadest[0], "name": broadest[1]["name"], "on_sale_pct": broadest[1]["on_sale_pct"]} if broadest else None,
        "active_campaigns": len(active), "your_on_sale_pct": per_site.get(source_key, {}).get("on_sale_pct"), "your_avg_discount_pct": per_site.get(source_key, {}).get("avg_discount_pct"),
    }


def nice_date(iso: str | None) -> str:
    if not iso:
        return ""
    d = date.fromisoformat(iso)
    return f"{d.day} {d.strftime('%b')}"


def _insights(p: dict, source_key: str, currency: str) -> tuple[list[dict], list[dict]]:
    ins: list[dict] = []
    recs: list[dict] = []
    sites, s = p["sites"], p["summary"]
    me = sites.get(source_key)
    comps = [(k, d) for k, d in sites.items() if k != source_key]
    measured = sorted([(k, d) for k, d in comps if d["on_sale"]], key=lambda kv: -kv[1]["on_sale_pct"])
    if measured:
        parts = [f"{d['name']}: {d['on_sale_pct']:g}% of products on sale, average {d['avg_discount_pct']:g}% off (up to {d['max_discount_pct']:g}%)" for _, d in measured[:4]]
        mine = f" Yours: {me['on_sale_pct']:g}% on sale" + (f", average {me['avg_discount_pct']:g}% off." if me and me["avg_discount_pct"] is not None else ".") if me else ""
        ins.append({"title": "Competitor discounting", "severity": "info", "text": "; ".join(parts) + "." + mine})
    if s["no_original_price"]:
        ins.append({"title": "Discounts not measurable", "severity": "info",
                    "text": f"{', '.join(s['no_original_price'])} do(es) not publish original ('was') prices on products, so product discounts could not be measured there. Their banner offers (if any) are listed below."})
    gaps = [r for r in p["types"] if r["gap_pts"] is not None and r["gap_pts"] >= 5 and r["competitors_on_sale"] >= 3]
    for r in sorted(gaps, key=lambda r: -r["gap_pts"])[:3]:
        name = sites[r["best_competitor"]]["name"]
        yours = f"your average is {r['yours']:g}% on {r['yours_on_sale']} item(s)" if r["yours"] is not None else "you have nothing on sale there"
        ins.append({"title": f"Deeper discounts on {r['type']}", "severity": "warning",
                    "text": f"{name} discounts {r['type']} by {r['best_competitor_avg']:g}% on average ({r['competitors_on_sale']} competitor items on sale); {yours}."})
        recs.append({"priority": "medium", "action": f"Consider a promotion on {r['type']}",
                     "detail": f"Competitors are {r['gap_pts']:g} points deeper than you on average ({name} {r['best_competitor_avg']:g}%). Check your margin before matching."})
    live = [c for c in p["campaigns"] if not c["is_source"] and c["status"] in ("active", "upcoming", "ongoing") and c["kind"] != "free_shipping"]
    if live:
        lines = []
        for c in live[:5]:
            when = (f", ends {nice_date(c['end'])} ({c['days_left']} day{'s' if c['days_left'] != 1 else ''} left)" if c["end"] and c["status"] == "active" and c["days_left"] is not None
                    else f", starts {nice_date(c['start'])}" if c["status"] == "upcoming" else "")
            lines.append(f"{c['site_name']}: {c['summary']}{when}")
        ins.append({"title": "Competitor campaigns running", "severity": "info", "text": "; ".join(lines) + "."})
    return ins, recs


# --------------------------------------------------------------------------- history (period measured over repeated runs)
def attach_promo_history(engine, promos: dict, sites: list[SiteData], run_id: int) -> None:
    """How long have today's discounts been running? Uses stored snapshots from earlier analyses: for each product on sale now, count back
    through earlier runs while it stayed on sale. The result is 'observed since', which is a lower bound when the sale started before our first look."""
    from sqlalchemy import select

    from src.db.models import Offer, PriceSnapshot, Product, Run, Site
    from src.db.session import session_scope

    hist: dict[str, dict] = {}
    with session_scope(engine) as s:
        cur_run = s.get(Run, run_id)
        if cur_run is None:
            return
        for sd in sites:
            if sd.status != "ok":
                continue
            site = s.scalar(select(Site).where(Site.key == sd.key))
            if site is None:
                continue
            rows = s.execute(select(Product.id, Product.external_id, PriceSnapshot.run_id, PriceSnapshot.price, PriceSnapshot.compare_at_price)
                             .join(PriceSnapshot, PriceSnapshot.product_id == Product.id).where(Product.site_id == site.id)).all()
            per: dict[str, list[tuple[int, bool]]] = {}
            for _pid, ext, rid, price, cmp_ in rows:
                per.setdefault(ext, []).append((rid, bool(price and cmp_ and cmp_ > price * 1.005)))
            run_ids = sorted({rid for v in per.values() for rid, _ in v})
            if len(run_ids) < 2:
                continue
            started = {rid: s.get(Run, rid).started_at for rid in run_ids}
            ptype = {p.external_id: infer_product_type(p) for p in sd.products}
            on_now = [e for e, v in per.items() if any(rid == run_id and sale for rid, sale in v)]
            prev_run = max((r for r in run_ids if r < run_id), default=None)
            ended = sum(1 for e, v in per.items() if prev_run and dict(v).get(prev_run) and not dict(v).get(run_id, False) and e in ptype)
            durations: list[float] = []
            new_now = 0
            by_type: dict[str, list[float]] = {}
            first_seen: list[datetime] = []
            for e in on_now:
                flags = dict(per[e])
                start_run = run_id
                for r in sorted((r for r in run_ids if r < run_id), reverse=True):
                    if flags.get(r):
                        start_run = r
                    else:
                        break
                days = max((cur_run.started_at - started[start_run]).total_seconds() / 86400, 0.0)
                durations.append(days)
                first_seen.append(started[start_run])
                by_type.setdefault(ptype.get(e, "Other"), []).append(days)
                new_now += start_run == run_id
            if not durations:
                continue
            oldest_run = run_ids[0]
            hist[sd.key] = {
                "runs_observed": len(run_ids), "observed_since": min(first_seen).date().isoformat(), "median_days_on_sale": round(statistics.median(durations), 1),
                "longest_days_on_sale": round(max(durations), 1), "new_discounts_this_run": new_now, "discounts_ended_since_last_run": ended,
                "lower_bound": started.get(oldest_run) is not None and min(first_seen) <= started[oldest_run],
                "by_type": {t: {"median_days": round(statistics.median(v), 1), "items": len(v)} for t, v in sorted(by_type.items(), key=lambda kv: -len(kv[1]))[:12]},
            }
            # banner campaigns: first/last seen across runs, matched on normalised text
            offer_rows = s.execute(select(Offer.text, Offer.run_id).where(Offer.site_id == site.id)).all()
            seen: dict[str, list[int]] = {}
            for t, rid in offer_rows:
                seen.setdefault(re.sub(r"\W+", " ", (t or "").lower()).strip(), []).append(rid)
            for c in promos["campaigns"]:
                if c["site_key"] != sd.key:
                    continue
                cnorm = re.sub(r"\W+", " ", c["text"].lower()).strip()
                # the wording of a banner drifts over time (a date gets added, a code changes), so combine every stored version of it
                rids = sorted({rid for k, v in seen.items() if k and (k in cnorm or cnorm[:60] in k) for rid in v})
                if rids and rids[0] in started:
                    c["first_seen"] = started[rids[0]].date().isoformat()
                    c["runs_seen"] = len(rids)
                    c["seen_days"] = round((cur_run.started_at - started[rids[0]]).total_seconds() / 86400, 1)
    promos["history"] = hist
    if hist:
        names = {k: promos["sites"][k]["name"] for k in hist if k in promos["sites"]}
        parts = [f"{names[k]}: current discounts have been running {h['median_days_on_sale']:g} days (median, up to {h['longest_days_on_sale']:g}) since {nice_date(h['observed_since'])}"
                 for k, h in hist.items() if k in names and k != promos.get("source_key") and h["longest_days_on_sale"] >= 1]
        if parts:
            promos["insights"].append({"title": "How long discounts run", "severity": "info",
                                       "text": "; ".join(parts[:4]) + ". Based on our repeated checks, so the real start may be earlier."})
