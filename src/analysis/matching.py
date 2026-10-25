"""Match competitor products to the source's products.

Priority: GTIN > SKU/MPN > (brand + shared model number) > fuzzy title. Matching is one-to-one per competitor.
Guards against classic false matches: different brands, conflicting model numbers (HP-100 vs HP-200) and
conflicting spec tokens (128GB vs 256GB) are rejected or penalised.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

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
