"""Per-product pricing recommendations.

For every product of yours that at least one competitor also sells (found by semantic matching) we work out:
  market average  = mean price of the matched competitor products, each expressed for YOUR pack size
  gap             = how far your price is above (+) or below (-) that average
  action          = decrease / increase / hold, with a concrete suggested price and the reason in plain words

Rules (all adjustable): inside +/-tolerance of the average -> hold. Above it -> lower to just under the average
(default 1% under). Below it -> raise by at most `max_increase_pct`, never beyond just under the average, because a
price a few percent below the market is leaving money on the table, but a big jump risks the sales that price wins.
Out-of-stock competitor prices are ignored when at least one in-stock price exists: nobody can buy at that price."""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass

from src.analysis.matching import Match
from src.analysis.money import fmt
from src.analysis.quantity import parse_quantity, unit_price
from src.analysis.taxonomy import infer_product_type
from src.analysis.types import SiteData
from src.scrapers.base import ProductRecord


@dataclass
class PricingOptions:
    tolerance_pct: float = 3.0
    max_increase_pct: float = 5.0
    undercut_pct: float = 1.0
    exclude_out_of_stock: bool = True
    max_price_ratio: float = 3.0  # a competitor price more than this many times above/below yours is treated as a different product / pack size


def _round_price(x: float, reference: float) -> float:
    """Whole units for big prices (rupees), cents for small ones."""
    return float(round(x)) if reference >= 100 else round(x, 2)


def _confidence_label(n: int, min_conf: float, spread: float | None) -> str:
    pts = (2 if n >= 3 else 1 if n == 2 else 0) + (2 if min_conf >= 0.92 else 1 if min_conf >= 0.85 else 0) + (1 if spread is not None and spread < 1.3 else 0)
    return "high" if pts >= 4 else "medium" if pts >= 2 else "low"


def _bucket(conf: float) -> str:
    return "<80" if conf < 0.80 else "80-85" if conf < 0.85 else "85-90" if conf < 0.90 else "90-95" if conf < 0.95 else "95-100"


def build_pricing(source: SiteData, comps: list[SiteData], matches: dict[str, list[Match]], *, currency: str = "INR",
                  options: PricingOptions | None = None) -> dict:
    opt = options or PricingOptions()
    by_source: dict[int, list[tuple[SiteData, Match]]] = defaultdict(list)
    for c in comps:
        for m in matches.get(c.key, []):
            if m.status == "matched":
                by_source[m.source_idx].append((c, m))

    items: list[dict] = []
    suspect_list: list[dict] = []
    for si, pairs in by_source.items():
        sp: ProductRecord = source.products[si]
        if not sp.price:
            continue
        s_qty = parse_quantity(sp.title)
        price = sp.price
        entries: list[dict] = []
        for c, m in pairs:
            cp = c.products[m.comp_idx]
            if not cp.price:
                continue
            adjusted = cp.price * m.size_ratio if m.size_relation == "scalable" and m.size_ratio else cp.price
            c_qty = parse_quantity(cp.title)
            c_unit = unit_price(cp.price, c_qty)
            entries.append({
                "compare_at": cp.compare_at_price, "unit_price": round(c_unit[0], 3) if c_unit else None, "unit_label": c_unit[1] if c_unit else None,
                "competitor_key": c.key, "competitor_name": c.name, "title": cp.title, "url": cp.url, "image": cp.image_url,
                "price": round(cp.price, 2), "adjusted_price": round(adjusted, 2), "size_adjusted": adjusted != cp.price,
                "size": c_qty.label if c_qty else None, "in_stock": cp.in_stock, "low_stock": cp.low_stock,
                "confidence": m.confidence, "title_sim": m.title_sim, "method": m.method, "reasons": list(m.reasons),
                "discount_pct": round((cp.compare_at_price - cp.price) / cp.compare_at_price * 100, 1) if cp.compare_at_price and cp.compare_at_price > cp.price else None,
            })
        if not entries:
            continue
        # Safety net: a competitor price several times above/below yours is almost never the same product (different pack size, missing
        # size in the title, a bundle...). Keep it OUT of the average and report it instead of recommending a 2,000% price cut.
        suspects = []
        for e in entries:
            hi_p, lo_p = max(price, e["adjusted_price"]), min(price, e["adjusted_price"])
            e["ratio"] = round(hi_p / lo_p, 1) if lo_p else None
            e["ignored"] = bool(lo_p and hi_p / lo_p > opt.max_price_ratio)
            if e["ignored"]:
                suspects.append(e)
        entries_ok = [e for e in entries if not e["ignored"]]
        for e in suspects:
            suspect_list.append({"title": sp.title, "price": round(price, 2), "size": s_qty.label if s_qty else None, "url": sp.url, "competitor_name": e["competitor_name"],
                                 "comp_title": e["title"], "comp_price": e["price"], "comp_adjusted": e["adjusted_price"], "comp_size": e["size"], "comp_url": e["url"],
                                 "ratio": e["ratio"], "confidence": e["confidence"]})
        if not entries_ok:
            continue
        in_stock = [e for e in entries_ok if e["in_stock"] is not False]
        used = in_stock if (opt.exclude_out_of_stock and in_stock) else entries_ok
        prices = [e["adjusted_price"] for e in used]
        avg, med, lo, hi = statistics.fmean(prices), statistics.median(prices), min(prices), max(prices)
        price = sp.price
        gap_abs = price - avg
        gap_pct = gap_abs / avg * 100
        position = "above" if gap_pct > opt.tolerance_pct else "below" if gap_pct < -opt.tolerance_pct else "aligned"
        suggested, action = price, "hold"
        if position == "above":
            suggested = _round_price(min(price, avg * (1 - opt.undercut_pct / 100)), price)
            action = "decrease" if suggested < price else "hold"
        elif position == "below":
            ceiling = avg * (1 - opt.undercut_pct / 100)
            suggested = _round_price(min(price * (1 + opt.max_increase_pct / 100), ceiling), price)
            action = "increase" if suggested > price else "hold"
        change_abs, change_pct = suggested - price, (suggested - price) / price * 100

        n = len(used)
        spread = hi / lo if lo else None
        min_conf = min(e["confidence"] for e in entries_ok)
        flags: list[str] = []
        if n == 1:
            flags.append("Only one competitor sells this, so the average is that single price")
        if n >= 2 and spread and spread >= 1.5:
            flags.append("Competitor prices vary a lot (highest is 1.5x the lowest). Check the matches")
        if any(e["size_adjusted"] for e in used):
            flags.append("Competitor pack sizes differ from yours, so their prices were scaled to your size")
        if len(used) < len(entries_ok):
            flags.append(f"{len(entries_ok) - len(used)} out-of-stock competitor price(s) ignored")
        if suspects:
            flags.append(f"{len(suspects)} competitor match(es) ignored: price more than {opt.max_price_ratio:g}x different from yours (probably a different pack size or product)")
        if sp.in_stock is False:
            flags.append("You are out of stock on this product")
        if min_conf < 0.88:
            flags.append("At least one match has moderate confidence. Verify before repricing")

        if position == "above":
            why = (f"Your price {fmt(price, currency)} is {gap_pct:.1f}% above the competitor average of {fmt(avg, currency)} ({n} competitor{'s' if n != 1 else ''}). "
                   + (f"Lowering it to {fmt(suggested, currency)} puts you {opt.undercut_pct:g}% under the average." if action == "decrease" else "No change is possible at this price."))
        elif position == "below":
            why = (f"Your price {fmt(price, currency)} is {abs(gap_pct):.1f}% below the competitor average of {fmt(avg, currency)} ({n} competitor{'s' if n != 1 else ''}). "
                   + (f"You can raise it to {fmt(suggested, currency)} ({change_pct:+.1f}%) and still be cheaper than the market." if action == "increase" else "There is no room to raise it safely."))
        else:
            why = f"Your price {fmt(price, currency)} is within ±{opt.tolerance_pct:g}% of the competitor average ({fmt(avg, currency)}). Keep it."
        ranked_prices = sorted(prices)
        items.append({
            "id": f"p{si}", "ext_id": sp.external_id, "title": sp.title, "brand": sp.brand, "sku": sp.sku, "url": sp.url, "image": sp.image_url,
            "category": infer_product_type(sp), "size": s_qty.label if s_qty else None, "in_stock": sp.in_stock,
            "price": round(price, 2), "compare_at": sp.compare_at_price,
            "competitors": sorted(entries, key=lambda e: (e["ignored"], e["adjusted_price"])), "n_competitors": n, "n_matches": len(entries_ok), "n_ignored": len(suspects),
            "low_stock": sp.low_stock, "discount_pct": round((sp.compare_at_price - sp.price) / sp.compare_at_price * 100, 1) if sp.compare_at_price and sp.compare_at_price > sp.price else None,
            "unit_price": round(s_unit[0], 3) if (s_unit := unit_price(price, s_qty)) else None, "unit_label": s_unit[1] if s_unit else None,
            "market_avg": round(avg, 2), "market_median": round(med, 2), "market_min": round(lo, 2), "market_max": round(hi, 2),
            "gap_abs": round(gap_abs, 2), "gap_pct": round(gap_pct, 1), "position": position,
            "action": action, "suggested_price": suggested, "change_abs": round(change_abs, 2), "change_pct": round(change_pct, 1),
            "rationale": why, "flags": flags, "confidence": _confidence_label(n, min_conf, spread), "match_confidence": round(statistics.fmean(e["confidence"] for e in entries_ok), 3),
            "min_match_confidence": min_conf, "rank": 1 + sum(1 for p in ranked_prices if p < price), "cheapest": price <= lo,
            "currency": currency,
        })
    items.sort(key=lambda it: -it["gap_pct"])
    summary = _summary(items, source, comps, matches, currency)
    summary["suspect_matches"] = len(suspect_list)
    summary["suspect_products"] = len({x["title"] for x in suspect_list})
    suspect_list.sort(key=lambda x: -(x["ratio"] or 0))
    return {"settings": asdict(opt), "currency": currency, "summary": summary, "categories": _categories(items, opt), "items": items, "suspect": suspect_list[:300]}


def _summary(items: list[dict], source: SiteData, comps: list[SiteData], matches: dict[str, list[Match]], currency: str) -> dict:
    above = [i for i in items if i["position"] == "above"]
    below = [i for i in items if i["position"] == "below"]
    all_matches = [e for i in items for e in i["competitors"] if not e.get("ignored")]
    pending = sum(1 for c in comps for m in matches.get(c.key, []) if m.status == "review")
    return {
        "currency": currency, "source_products": len(source.products), "competitor_products": {c.key: len(c.products) for c in comps},
        "matched_products": len(items), "competitor_matches": len(all_matches),
        "coverage_pct": round(len(items) / len(source.products) * 100, 1) if source.products else 0.0,
        "avg_match_confidence": round(statistics.fmean(e["confidence"] for e in all_matches) * 100, 1) if all_matches else None,
        "above": len(above), "below": len(below), "aligned": len(items) - len(above) - len(below),
        "decrease": sum(i["action"] == "decrease" for i in items), "increase": sum(i["action"] == "increase" for i in items), "hold": sum(i["action"] == "hold" for i in items),
        "overpriced_gap_total": round(sum(i["gap_abs"] for i in above), 2), "headroom_total": round(sum(-i["gap_abs"] for i in below), 2),
        "avg_gap_pct": round(statistics.fmean(i["gap_pct"] for i in items), 1) if items else None,
        "cheapest_count": sum(1 for i in items if i["cheapest"]), "needs_review": pending,
        "confidence_buckets": dict(Counter(_bucket(e["confidence"]) for e in all_matches)),
        "matches_per_competitor": dict(Counter(e["competitor_key"] for e in all_matches)),
    }


def _categories(items: list[dict], opt: PricingOptions) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        groups[it["category"]].append(it)
    out = []
    for cat, g in groups.items():
        avg_gap = statistics.fmean(i["gap_pct"] for i in g)
        dec, inc = [i for i in g if i["action"] == "decrease"], [i for i in g if i["action"] == "increase"]
        if avg_gap > opt.tolerance_pct:
            verdict = f"Reduce: you are {avg_gap:.1f}% above competitors on average"
        elif avg_gap < -opt.tolerance_pct:
            verdict = f"Room to raise: you are {abs(avg_gap):.1f}% below competitors on average"
        else:
            verdict = "Competitive: close to the competitor average"
        out.append({
            "category": cat, "products": len(g), "avg_gap_pct": round(avg_gap, 1),
            "avg_your_price": round(statistics.fmean(i["price"] for i in g), 2), "avg_market_price": round(statistics.fmean(i["market_avg"] for i in g), 2),
            "above": sum(i["position"] == "above" for i in g), "below": sum(i["position"] == "below" for i in g),
            "aligned": sum(i["position"] == "aligned" for i in g), "decrease": len(dec), "increase": len(inc),
            "avg_decrease_pct": round(statistics.fmean(i["change_pct"] for i in dec), 1) if dec else None,
            "avg_increase_pct": round(statistics.fmean(i["change_pct"] for i in inc), 1) if inc else None,
            "verdict": verdict, "cheapest": sum(1 for i in g if i["cheapest"]),
        })
    out.sort(key=lambda c: -c["products"])
    return out
