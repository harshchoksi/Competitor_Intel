"""Reading products from pages that have no schema.org markup.

Two free fallbacks, used only when the structured-data readers found nothing:

  * html_heuristic_product    a classic product page: a heading, a visible price, an "Add to cart" button. Reads title, price, the crossed-out
                              original price, stock wording and the main image straight from the visible HTML.
  * embedded_json_products    pages built by JavaScript frameworks (Next.js, Nuxt, Gatsby...) often ship their data inside the HTML as JSON.
                              We look through those JSON blocks for lists of things that have a name and a price.

Neither needs a browser. Both are conservative (cues required, minimum counts) so ordinary pages are not mistaken for products."""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from src.scrapers.base import ProductRecord, parse_price

_CURRENCY_SYMBOLS = {"€": "EUR", "£": "GBP", "₹": "INR", "afl": "AWG", "awg": "AWG", "rs": "INR", "usd": "USD", "us$": "USD"}
_AMOUNT = re.compile(r"(?:(?P<sym>US\$|USD|AWG|Afl\.?|Rs\.?|₹|€|£|\$)\s*)?(?P<num>\d{1,3}(?:[,\s]\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)(?:\s*(?P<post>USD|AWG|Afl|€|£))?", re.I)
_CART_CUE = re.compile(r"add\s+to\s+(?:cart|basket|bag|order)|buy\s+now|in\s+stock|out\s+of\s+stock|sold\s+out|add\s+to\s+wishlist|\bsku\b", re.I)
_OOS = re.compile(r"out\s+of\s+stock|sold\s+out|currently\s+unavailable|not\s+available", re.I)
_IN = re.compile(r"in\s+stock|add\s+to\s+(?:cart|basket|bag|order)|available", re.I)
_OLD_CLS = re.compile(r"old|was|regular|compare|original|strike|before|list-?price|msrp|crossed", re.I)
_PRICE_CLS = re.compile(r"price|amount|cost", re.I)


def _currency(sym: str | None, default: str) -> str:
    return _CURRENCY_SYMBOLS.get((sym or "").lower().rstrip("."), default) if sym and sym != "$" else default


def _amount(text: str, default_cur: str) -> tuple[float | None, str]:
    for m in _AMOUNT.finditer(text or ""):
        if not (m.group("sym") or m.group("post")):
            continue  # a bare number is not a price
        val = parse_price(m.group("num").replace(" ", ""))
        if val and val < 1_000_000:
            return val, _currency(m.group("sym") or m.group("post"), default_cur)
    return None, default_cur


def html_heuristic_product(html: str, url: str, default_currency: str) -> ProductRecord | None:
    """A single product from a plain product page, or None if the page does not look like one."""
    soup = BeautifulSoup(html or "", "lxml")
    text = soup.get_text(" ", strip=True)
    og_type = (soup.find("meta", attrs={"property": "og:type"}) or {}).get("content", "")
    if not (_CART_CUE.search(text) or og_type == "product"):
        return None
    h1 = soup.find("h1")
    title = (h1.get_text(" ", strip=True) if h1 else "") or (soup.find("meta", attrs={"property": "og:title"}) or {}).get("content", "")
    if not title or len(title) > 250:
        return None
    price = was = None
    currency = default_currency
    meta_price = soup.find(attrs={"itemprop": "price"})
    if meta_price is not None:
        price = parse_price(meta_price.get("content") or meta_price.get_text())
    candidates = [el for el in soup.find_all(["span", "div", "p", "ins", "bdi", "strong", "b"], class_=_PRICE_CLS)
                  if len(el.get_text(strip=True)) <= 40 and not el.find(["span", "div"], class_=_PRICE_CLS)]
    for el in candidates:
        cls = " ".join(el.get("class", []))
        if _OLD_CLS.search(cls) or el.find_parent(["del", "s", "strike"]):
            if was is None:
                was, _ = _amount(el.get_text(" ", strip=True), default_currency)
            continue
        if price is None:
            price, currency = _amount(el.get_text(" ", strip=True), default_currency)
    for el in soup.find_all(["del", "s", "strike"]):
        if was is None:
            was, _ = _amount(el.get_text(" ", strip=True), default_currency)
    if price is None:
        return None
    if was is not None and was <= price:
        was = None
    sku_el = soup.find(attrs={"itemprop": "sku"})
    sku = (sku_el.get("content") or sku_el.get_text(strip=True)) if sku_el else None
    if not sku:
        m = re.search(r"\bSKU\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9._\-]{2,40})", text)
        sku = m.group(1) if m else None
    image = (soup.find("meta", attrs={"property": "og:image"}) or {}).get("content")
    in_stock = False if _OOS.search(text) else (True if _IN.search(text) else None)
    path = urlparse(url).path.rstrip("/") or url
    return ProductRecord(external_id=sku or path, url=url, title=title[:500], sku=sku, price=price, compare_at_price=was, currency=currency,
                         in_stock=in_stock, image_url=urljoin(url, image) if image else None)


# --------------------------------------------------------------------------- data embedded in JavaScript-app pages
_NAME_KEYS = ("name", "title", "productName", "product_name", "displayName", "label")
_PRICE_KEYS = ("price", "salePrice", "sale_price", "currentPrice", "current_price", "offerPrice", "finalPrice", "unitPrice", "regularPrice", "priceAmount", "amount")
_WAS_KEYS = ("compareAtPrice", "compare_at_price", "originalPrice", "original_price", "oldPrice", "old_price", "wasPrice", "listPrice", "msrp", "regularPrice")
_ID_KEYS = ("id", "productId", "product_id", "sku", "slug", "handle", "code", "uuid")
_IMG_KEYS = ("image", "imageUrl", "image_url", "thumbnail", "img", "photo", "picture")
_STOCK_KEYS = ("available", "inStock", "in_stock", "isAvailable", "is_available", "inventoryAvailable")
_ASSIGN = re.compile(r"window\.__[A-Za-z0-9_]+__\s*=\s*")


def _json_blobs(html: str) -> list:
    soup = BeautifulSoup(html or "", "lxml")
    blobs = []
    for sc in soup.find_all("script"):
        body = (sc.string or sc.get_text() or "").strip()
        if not body:
            continue
        if sc.get("type") in ("application/json", "application/ld+json") or sc.get("id") in ("__NEXT_DATA__", "__NUXT_DATA__", "__APOLLO_STATE__"):
            try:
                blobs.append(json.loads(body))
            except ValueError:
                continue
        else:
            for m in _ASSIGN.finditer(body):
                try:
                    obj, _ = json.JSONDecoder().raw_decode(body[m.end():])
                    blobs.append(obj)
                except ValueError:
                    continue
    return blobs


def _num(v) -> float | None:
    if isinstance(v, dict):
        for k in ("amount", "value", "current", "price", "centAmount"):
            if k in v:
                return _num(v[k])
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if 0 < v < 1_000_000 else None
    if isinstance(v, str) and re.fullmatch(r"\s*[^\d\s]{0,4}\s*\d[\d,]*(?:\.\d+)?\s*", v):
        return parse_price(v)
    return None


def _first(d: dict, keys: tuple) -> object:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _walk(obj, out: list[dict], depth: int = 0) -> None:
    if depth > 14:
        return
    if isinstance(obj, dict):
        name, price = _first(obj, _NAME_KEYS), _num(_first(obj, _PRICE_KEYS))
        if isinstance(name, str) and 3 <= len(name) <= 200 and price and _first(obj, _ID_KEYS) is not None:
            out.append(obj)
        for v in obj.values():
            _walk(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, out, depth + 1)


def embedded_json_products(html: str, url: str, default_currency: str, minimum: int = 4) -> list[ProductRecord]:
    """Products found in JSON blocks embedded in the page. Needs at least `minimum` of them, so a stray 'name + price' is ignored."""
    found: list[dict] = []
    for blob in _json_blobs(html):
        _walk(blob, found)
    records: dict[str, ProductRecord] = {}
    for d in found:
        name = str(_first(d, _NAME_KEYS)).strip()
        price = _num(_first(d, _PRICE_KEYS))
        was = _num(_first(d, _WAS_KEYS))
        raw_id = _first(d, _ID_KEYS)
        ext = str(raw_id) if raw_id is not None else hashlib.md5(name.encode()).hexdigest()[:12]
        img = _first(d, _IMG_KEYS)
        if isinstance(img, dict):
            img = img.get("url") or img.get("src")
        elif isinstance(img, list) and img:
            img = img[0].get("url") if isinstance(img[0], dict) else img[0]
        stock = _first(d, _STOCK_KEYS)
        link = d.get("url") or d.get("permalink") or d.get("link")
        records.setdefault(ext, ProductRecord(
            external_id=ext, title=name[:500], url=urljoin(url, link) if isinstance(link, str) else url, price=price,
            compare_at_price=was if (was and price and was > price) else None, currency=default_currency,
            in_stock=stock if isinstance(stock, bool) else None, image_url=urljoin(url, img) if isinstance(img, str) else None, sku=str(d["sku"]) if d.get("sku") else None))
    return list(records.values()) if len(records) >= minimum else []
