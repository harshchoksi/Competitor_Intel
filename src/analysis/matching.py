"""Match competitor products to the source's products.

The heavy lifting is in semantic.py (identifiers, synonyms, IDF-weighted text similarity, optional embeddings, logic
guards and pack-size awareness). This module keeps the public API: `match_products` and the older category-level
comparison (`match_by_category`) used by the PDF report.

Matching is one-to-one per competitor: every competitor product is used at most once, by its best partner."""
from __future__ import annotations

import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache

from rapidfuzz import fuzz

from src.analysis.semantic import (  # noqa: F401  (re-exported: tests and other modules import these from here)
    Embedder,
    Profile,
    build_profiles,
    model_tokens,
    norm_gtin,
    norm_sku,
    semantic_match,
    spec_tokens,
)
from src.scrapers.base import ProductRecord


@dataclass(frozen=True)
class Match:
    source_idx: int
    comp_idx: int
    confidence: float
    method: str  # gtin | sku | model | fuzzy | semantic | ai
    status: str  # matched | review
    title_sim: float | None = None  # 0-1 text similarity (what the dashboard shows as "title similarity")
    size_relation: str = "unknown"  # same | scalable | unknown | incompatible
    size_ratio: float | None = None  # your pack size / their pack size (when scalable)
    reasons: tuple[str, ...] = ()  # human-readable "why we think these are the same"


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


def match_profiles(
    source_profiles: Sequence[Profile], comp: Sequence[ProductRecord], threshold: float = 0.85, review_floor: float | None = None,
    embedder: Embedder | None = None,
) -> list[Match]:
    """Match using prebuilt source profiles (build them once, reuse for every competitor)."""
    floor = review_floor if review_floor is not None else max(0.30, min(0.70, threshold - 0.10))
    comp_profiles = build_profiles(comp)
    out = []
    for i, j, s, status in semantic_match(source_profiles, comp_profiles, threshold=threshold, review_floor=floor, embedder=embedder):
        out.append(Match(i, j, s.conf, s.method, status, s.title_sim, s.size_relation, s.size_ratio, s.reasons))
    return out


def match_products(
    source: list[ProductRecord], comp: list[ProductRecord], threshold: float = 0.85, review_floor: float | None = None,
    embedder: Embedder | None = None,
) -> list[Match]:
    """Pair each competitor product with the source product it most plausibly is. Unsure pairs get status 'review'."""
    return match_profiles(build_profiles(source), comp, threshold, review_floor, embedder)


# ---------------------------------------------------------------------------
# Category-level matching (brand-vs-brand)
# ---------------------------------------------------------------------------

# Types too generic to use as a category label — fall through to tags instead
_GENERIC_TYPES = {
    "clothing", "accessories", "apparel", "other", "misc", "product",
    "gift", "bundle", "set", "collection", "sale", "new",
}
# Canonical names for common garment / product keywords
_NORM_MAP = {
    "tank": "Tank Top", "tanks": "Tank Top", "cami": "Tank Top", "camisole": "Tank Top",
    "sleeveless": "Tank Top", "singlet": "Tank Top",
    "t_shirt": "T-Shirt", "tshirt": "T-Shirt", "tee": "T-Shirt", "tees": "T-Shirt",
    "legging": "Leggings", "leggings": "Leggings", "tight": "Leggings", "tights": "Leggings",
    "short": "Shorts", "shorts": "Shorts",
    "bra": "Sports Bra", "sports_bra": "Sports Bra", "sports bra": "Sports Bra",
    "hoodie": "Hoodie", "hoody": "Hoodie", "sweatshirt": "Hoodie",
    "jacket": "Jacket", "track_top": "Jacket", "zip": "Jacket",
    "jogger": "Joggers", "joggers": "Joggers", "track": "Track Pants", "sweatpants": "Joggers",
    "vest": "Vest",
    "top": "Top", "ls_top": "Long Sleeve Top", "ls top": "Long Sleeve Top",
    "crop": "Crop Top", "crop_top": "Crop Top",
    "polo": "Polo", "shirt": "Shirt",
    "dress": "Dress", "skirt": "Skirt",
    "sock": "Socks", "socks": "Socks",
    "shoe": "Shoes", "shoes": "Shoes", "trainer": "Shoes", "sneaker": "Shoes",
    "bag": "Bags", "tote": "Bags", "backpack": "Bags",
    "bottle": "Water Bottles", "shaker": "Shakers",
    "protein": "Protein", "whey": "Protein", "creatine": "Creatine",
    "bcaa": "BCAA", "supplement": "Supplements", "vitamin": "Vitamins",
    "watch": "Smartwatch", "smartwatch": "Smartwatch",
    "earphone": "Earphones", "earbud": "Earphones", "headphone": "Headphones",
    "speaker": "Speakers",
}
_HIERARCHY_SEP = re.compile(r"[>|/\\]")
_TAG_SKIP = re.compile(
    r"^(\d|>|<|sale|new|%|bestsell|featured|all|deal|offer|latest|afterpay|activity:|akeneo|category-|\d{2}/)",
    re.I,
)
# Keywords that indicate a tag is a garment/product type, not metadata
_GARMENT_KW = re.compile(
    r"legging|tight|short|bra|top|jacket|hoodie|vest|jogger|tank|pant|shirt|dress|skirt|sock|shoe|bag|tote|bottle|shaker|protein|whey|creatine|bcaa|watch|earbud|earphone|headphone|speaker",
    re.I,
)


def _normalize_leaf(raw: str) -> str | None:
    """Normalize a raw leaf category string to a canonical label."""
    key = raw.strip().lower().replace("-", "_").replace(" ", "_")
    if key in _NORM_MAP:
        return _NORM_MAP[key]
    # Try space-separated version too
    key2 = raw.strip().lower()
    if key2 in _NORM_MAP:
        return _NORM_MAP[key2]
    # If not in map and not generic, return title-cased as-is
    if key not in _GENERIC_TYPES and key2 not in _GENERIC_TYPES and len(raw.strip()) > 2:
        return raw.strip().title()
    return None


_PHRASE_RX: list[tuple[re.Pattern, str]] | None = None


def _phrase_patterns() -> list[tuple[re.Pattern, str]]:
    """Longest phrases first ("sports bra" before "bra"), compiled once instead of on every product."""
    global _PHRASE_RX
    if _PHRASE_RX is None:
        _PHRASE_RX = [(re.compile(r"\b" + re.escape(ph.replace("_", " ")) + r"\b"), _NORM_MAP[ph]) for ph in sorted(_NORM_MAP, key=len, reverse=True)]
    return _PHRASE_RX


def _infer_category(p: ProductRecord) -> str | None:
    return _infer_category_cached(p.category or "", tuple(p.tags or ()), p.title)


@lru_cache(maxsize=200_000)
def _infer_category_cached(category: str, tags: tuple, title: str) -> str | None:
    """Return the best available category label for a product, normalised.

    Strategy:
    1. Use product_type — if hierarchical (e.g. Gymshark's "Womens>Apparel>Tops>tanks")
       extract the most-specific (last) segment and normalize it.
    2. If product_type is blank or too generic ("Clothing", "Accessories"),
       fall back to tags: first try to find a tag matching a known garment keyword,
       then fall back to the first tag that looks like a product type.
    3. Last resort: scan the product title for known category keywords.
    """
    raw = category.strip()
    if raw:
        # Split on hierarchy separators and take the most specific (last) non-empty part
        parts = [x.strip() for x in _HIERARCHY_SEP.split(raw) if x.strip()]
        for leaf in reversed(parts):  # most specific first
            normalized = _normalize_leaf(leaf)
            if normalized:
                return normalized

    # Fall back to tags
    for tag in tags:
        tag = tag.strip()
        if not tag or _TAG_SKIP.search(tag) or len(tag) < 3 or len(tag) > 50:
            continue
        # Direct map hit
        key = tag.lower().replace("-", "_").replace(" ", "_")
        if key in _NORM_MAP:
            return _NORM_MAP[key]
        if tag.lower() in _NORM_MAP:
            return _NORM_MAP[tag.lower()]
        # Tag contains a garment keyword → use it as the category
        if _GARMENT_KW.search(tag):
            return tag.title()

    # Last resort: scan product title for known category keywords
    title_lower = title.lower()
    for rx, label in _phrase_patterns():
        if rx.search(title_lower):
            return label

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


