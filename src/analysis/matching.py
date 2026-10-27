"""Match competitor products to the source's products.

Priority: GTIN > SKU/MPN > (brand + shared model number) > fuzzy title. Matching is one-to-one per competitor.
Guards against classic false matches: different brands, conflicting model numbers (HP-100 vs HP-200) and
conflicting spec tokens (128GB vs 256GB) are rejected or penalised.

Category matching: groups products by product_type/category so brand-vs-brand comparisons work even when
individual product names differ (e.g. "Gymshark Leggings" vs "Ryderwear Leggings" both map to "Leggings").
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from src.scrapers.base import ProductRecord

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[-/][A-Za-z0-9]+)*")
_UNIT_RE = re.compile(r"^\d+(\.\d+)?(gb|tb|mb|mah|w|mm|cm|inch|in|kg|g|ml|l|hz|ghz|mp|v|x|pcs|pack)$", re.I)
_ALNUM = re.compile(r"[^a-z0-9]")


@dataclass(frozen=True)
class Match:
    source_idx: int
    comp_idx: int
    confidence: float
    method: str  # gtin | sku | model | fuzzy
    status: str  # matched | review


@dataclass
class CategoryMatch:
    """Aggregate price comparison for one product category across source vs a competitor."""
    category: str
    source_count: int
    comp_count: int
    source_avg_price: float | None
    comp_avg_price: float | None
    source_min_price: float | None
    comp_min_price: float | None
    source_max_price: float | None
    comp_max_price: float | None
    price_gap_pct: float | None        # positive = competitor is more expensive
    source_on_sale_pct: float | None
    comp_on_sale_pct: float | None
    source_products: list[dict] = field(default_factory=list)
    comp_products: list[dict] = field(default_factory=list)


def _norm(s: str | None) -> str:
    return _ALNUM.sub("", (s or "").lower())


def norm_gtin(g: str | None) -> str | None:
    digits = re.sub(r"\D", "", g or "").lstrip("0")
    return digits if len(digits) >= 8 else None


def norm_sku(s: str | None) -> str | None:
    n = _norm(s)
    return n if len(n) >= 4 and any(c.isdigit() for c in n) else None


def model_tokens(title: str) -> set[str]:
    """Tokens mixing letters and digits (HP-100, WH1000XM5), excluding capacities/units (128GB, 5000mAh)."""
    out = set()
    for tok in _TOKEN_RE.findall(title):
        if _UNIT_RE.match(tok):
            continue
        if any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok):
            out.add(_norm(tok))
    return out


def spec_tokens(title: str) -> set[str]:
    """Pure numbers and number+unit tokens (256GB, 42mm, 2024)."""
    return {_norm(t) for t in _TOKEN_RE.findall(title) if t.isdigit() or _UNIT_RE.match(t)}


def _clean_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", title.lower())


def _brand_compatible(a: str | None, b: str | None) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return True  # unknown brand: allowed, but fuzzy-only matches stay strict
    return na == nb or na in nb or nb in na


def _fuzzy(a: str, b: str) -> float:
    ca, cb = _clean_title(a), _clean_title(b)
    return (fuzz.token_set_ratio(ca, cb) + fuzz.token_sort_ratio(ca, cb)) / 200.0


def match_products(
    source: list[ProductRecord], comp: list[ProductRecord], threshold: float = 0.85, review_floor: float = 0.70
) -> list[Match]:
    s_gtin = [norm_gtin(p.gtin) for p in source]
    s_sku = [norm_sku(p.sku) or norm_sku(p.mpn) for p in source]
    s_models = [model_tokens(p.title) for p in source]
    s_specs = [spec_tokens(p.title) for p in source]

    candidates: dict[tuple[int, int], tuple[float, str]] = {}

    def offer(i: int, j: int, conf: float, method: str) -> None:
        if conf > candidates.get((i, j), (0.0, ""))[0]:
            candidates[(i, j)] = (conf, method)

    for j, cp in enumerate(comp):
        c_gtin, c_sku = norm_gtin(cp.gtin), norm_sku(cp.sku) or norm_sku(cp.mpn)
        c_models, c_specs = model_tokens(cp.title), spec_tokens(cp.title)
        for i, sp in enumerate(source):
            if c_gtin and c_gtin == s_gtin[i]:
                offer(i, j, 1.0, "gtin")
                continue
            if not _brand_compatible(sp.brand, cp.brand):
                continue
            if c_sku and c_sku == s_sku[i]:
                offer(i, j, 0.97, "sku")
                continue
            fz = _fuzzy(sp.title, cp.title)
            shared = s_models[i] & c_models
            if shared:
                brands_known = bool(_norm(sp.brand) and _norm(cp.brand))
                if brands_known or fz >= 0.6:
                    offer(i, j, 0.90 + 0.06 * fz, "model")
                continue
            if s_models[i] and c_models:  # both name a model, but different ones -> different products
                continue
            conf = fz * 0.95
            if s_specs[i] and c_specs and s_specs[i] != c_specs:
                conf *= 0.85
            if conf >= review_floor:
                offer(i, j, conf, "fuzzy")

    matches: list[Match] = []
    used_i: set[int] = set()
    used_j: set[int] = set()
    for (i, j), (conf, method) in sorted(candidates.items(), key=lambda kv: -kv[1][0]):
        if i in used_i or j in used_j:
            continue
        used_i.add(i)
        used_j.add(j)
        matches.append(Match(i, j, round(conf, 3), method, "matched" if conf >= threshold else "review"))
    return matches


# ---------------------------------------------------------------------------
# Category-level matching (brand-vs-brand)
# ---------------------------------------------------------------------------

def _infer_category(p: ProductRecord) -> str | None:
    """Return the best available category label for a product, normalised to title-case."""
    raw = p.category or None
    if raw and raw.strip():
        return raw.strip().title()
    # Fall back to first meaningful tag (skip size/colour/discount tags)
    _skip = re.compile(r"^\d|sale|new|%|bestsell|featured|all|deal|offer|latest", re.I)
    for tag in p.tags:
        tag = tag.strip()
        if tag and not _skip.match(tag) and len(tag) > 2:
            return tag.title()
    return None


def _on_sale_pct(products: list[ProductRecord]) -> float | None:
    n = len(products)
    if not n:
        return None
    on_sale = sum(
        1 for p in products
        if p.price and p.compare_at_price and p.compare_at_price > p.price
    )
    return round(on_sale / n * 100, 1)


def _avg_price(products: list[ProductRecord]) -> float | None:
    prices = [p.price for p in products if p.price]
    return round(statistics.fmean(prices), 2) if prices else None


def _product_stub(p: ProductRecord) -> dict:
    return {"title": p.title, "price": p.price, "url": p.url, "in_stock": p.in_stock}


def match_by_category(
    source: list[ProductRecord],
    comp: list[ProductRecord],
    min_products: int = 1,
) -> list[CategoryMatch]:
    """
    Group source and competitor products by category/product_type, then compute
    aggregate price comparison per shared category.  Works even when individual
    product names are completely different (brand-vs-brand scenario).
    """
    # Build category buckets
    src_buckets: dict[str, list[ProductRecord]] = {}
    for p in source:
        cat = _infer_category(p)
        if cat:
            src_buckets.setdefault(cat, []).append(p)

    cmp_buckets: dict[str, list[ProductRecord]] = {}
    for p in comp:
        cat = _infer_category(p)
        if cat:
            cmp_buckets.setdefault(cat, []).append(p)

    # Try exact match first, then fuzzy category name match
    matched: list[CategoryMatch] = []
    used_cmp_cats: set[str] = set()

    for src_cat, src_prods in src_buckets.items():
        # 1. Exact match
        cmp_prods = cmp_buckets.get(src_cat)
        cmp_cat = src_cat
        if cmp_prods is None:
            # 2. Fuzzy category name match (>= 80% similarity)
            best_score, best_cat = 0.0, None
            for cc in cmp_buckets:
                if cc in used_cmp_cats:
                    continue
                score = fuzz.token_set_ratio(src_cat.lower(), cc.lower()) / 100
                if score > best_score:
                    best_score, best_cat = score, cc
            if best_score >= 0.80 and best_cat:
                cmp_prods = cmp_buckets[best_cat]
                cmp_cat = best_cat

        if cmp_prods is None:
            continue
        if len(src_prods) < min_products and len(cmp_prods) < min_products:
            continue

        used_cmp_cats.add(cmp_cat)
        src_avg = _avg_price(src_prods)
        cmp_avg = _avg_price(cmp_prods)
        gap = None
        if src_avg and cmp_avg:
            gap = round((cmp_avg - src_avg) / src_avg * 100, 1)  # +ve = comp is dearer

        src_prices = [p.price for p in src_prods if p.price]
        cmp_prices = [p.price for p in cmp_prods if p.price]

        matched.append(CategoryMatch(
            category=src_cat,
            source_count=len(src_prods),
            comp_count=len(cmp_prods),
            source_avg_price=src_avg,
            comp_avg_price=cmp_avg,
            source_min_price=min(src_prices) if src_prices else None,
            comp_min_price=min(cmp_prices) if cmp_prices else None,
            source_max_price=max(src_prices) if src_prices else None,
            comp_max_price=max(cmp_prices) if cmp_prices else None,
            price_gap_pct=gap,
            source_on_sale_pct=_on_sale_pct(src_prods),
            comp_on_sale_pct=_on_sale_pct(cmp_prods),
            source_products=[_product_stub(p) for p in src_prods[:5]],
            comp_products=[_product_stub(p) for p in cmp_prods[:5]],
        ))

    matched.sort(key=lambda m: abs(m.price_gap_pct or 0), reverse=True)
    return matched


