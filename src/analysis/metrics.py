"""Pure-code metrics (no LLM): exact, repeatable numbers for the dashboard and report."""
from __future__ import annotations

import statistics
from datetime import UTC, datetime

from src.ads.links import ad_library_links
from src.analysis.matching import Match, match_products
from src.analysis.types import SiteData
from src.scrapers.base import ProductRecord

SAME_TOLERANCE_PCT = 1.0


def discount_pct(p: ProductRecord) -> float | None:
    if p.price and p.compare_at_price and p.compare_at_price > p.price:
        return round((p.compare_at_price - p.price) / p.compare_at_price * 100, 1)
    return None


def _r(x: float | None, n: int = 1) -> float | None:
    return None if x is None else round(x, n)


def site_stats(sd: SiteData) -> dict:
    ps = sd.products
    prices = [p.price for p in ps if p.price]
    discounts = [d for p in ps if (d := discount_pct(p)) is not None]
    ratings = [p.rating for p in ps if p.rating]
    n = len(ps)
    return {
        "key": sd.key, "name": sd.name, "role": "source" if sd.is_source else "competitor",
        "status": sd.status, "platform": sd.platform, "method": sd.method, "error": sd.error,
        "product_count": n,
        "avg_price": _r(statistics.fmean(prices)) if prices else None,
        "median_price": _r(statistics.median(prices)) if prices else None,
        "min_price": min(prices) if prices else None, "max_price": max(prices) if prices else None,
        "on_sale_count": len(discounts),
        "on_sale_pct": _r(len(discounts) / n * 100) if n else None,
        "avg_discount_pct": _r(statistics.fmean(discounts)) if discounts else None,
        "max_discount_pct": max(discounts) if discounts else None,
        "in_stock": sum(p.in_stock is True for p in ps),
        "out_of_stock": sum(p.in_stock is False for p in ps),
        "unknown_stock": sum(p.in_stock is None for p in ps),
        "low_stock": sum(p.low_stock for p in ps),
        "rated_count": len(ratings),
        "avg_rating": _r(statistics.fmean(ratings), 2) if ratings else None,
    }


def _side(p: ProductRecord) -> dict:
    return {"ext_id": p.external_id, "price": p.price, "compare_at": p.compare_at_price, "discount_pct": discount_pct(p),
            "in_stock": p.in_stock, "low_stock": p.low_stock, "title": p.title, "url": p.url, "rating": p.rating}


def compare(source: SiteData, comps: list[SiteData], matches: dict[str, list[Match]]):
    rows: dict[int, dict] = {}
    per_comp: dict[str, dict] = {}
    for c in comps:
        gaps: list[float] = []
        cheaper = same = costlier = 0
        for m in matches.get(c.key, []):
            if m.status != "matched":
                continue
            sp, cp = source.products[m.source_idx], c.products[m.comp_idx]
            if not sp.price or not cp.price:
                continue
            gap_pct = (cp.price - sp.price) / sp.price * 100  # + = competitor is dearer than you
            gaps.append(gap_pct)
            if gap_pct < -SAME_TOLERANCE_PCT:
                cheaper += 1
            elif gap_pct > SAME_TOLERANCE_PCT:
                costlier += 1
            else:
                same += 1
            row = rows.setdefault(m.source_idx, {"source": _side(sp), "brand": sp.brand, "category": sp.category, "competitors": {}})
            row["competitors"][c.key] = {**_side(cp), "gap_pct": round(gap_pct, 1), "gap_abs": round(cp.price - sp.price, 2),
                                         "confidence": m.confidence, "method": m.method}
        per_comp[c.key] = {
            "key": c.key, "name": c.name, "matched": len(gaps),
            "price_index": _r(100 + statistics.fmean(gaps)) if gaps else None,  # you = 100
            "avg_gap_pct": _r(statistics.fmean(gaps)) if gaps else None,
            "cheaper": cheaper, "same": same, "costlier": costlier,
        }
    out_rows = []
    for row in rows.values():
        prices = {"__source__": row["source"]["price"], **{k: v["price"] for k, v in row["competitors"].items()}}
        cheapest = min(prices, key=lambda k: prices[k])
        row["cheapest"] = None if cheapest == "__source__" else cheapest
        row["source_rank"] = 1 + sum(1 for k, v in prices.items() if k != "__source__" and v < prices["__source__"])
        out_rows.append(row)
    out_rows.sort(key=lambda r: r["source"]["title"].lower())
    return out_rows, per_comp


def build_alerts(source: SiteData, comps: list[SiteData], rows: list[dict], undercut_alert_pct: float) -> list[dict]:
    names = {c.key: c.name for c in comps}
    alerts: list[dict] = []
    for r in rows:
        title = r["source"]["title"]
        for k, v in r["competitors"].items():
            if v["gap_pct"] <= -undercut_alert_pct:
                alerts.append({"type": "undercut", "level": "warning", "site": names[k], "product": title, "value": v["gap_pct"],
                               "message": f"{names[k]} is {abs(v['gap_pct']):.0f}% cheaper on {title} (₹{v['price']:,.0f} vs your ₹{r['source']['price']:,.0f})"})
            if v["in_stock"] is False and r["source"]["in_stock"] is True:
                alerts.append({"type": "competitor_out_of_stock", "level": "opportunity", "site": names[k], "product": title,
                               "message": f"{names[k]} is out of stock on {title} while you have it. Consider promoting it"})
            elif v["in_stock"] is True and r["source"]["in_stock"] is False:
                alerts.append({"type": "you_out_of_stock", "level": "risk", "site": names[k], "product": title,
                               "message": f"You are out of stock on {title}, but {names[k]} has it at ₹{v['price']:,.0f}"})
            if v["low_stock"]:
                alerts.append({"type": "competitor_low_stock", "level": "info", "site": names[k], "product": title,
                               "message": f"{names[k]} shows low stock on {title}"})
    for c in comps:
        for p in c.products:
            d = discount_pct(p)
            if d is not None and d >= 30:
                alerts.append({"type": "big_discount", "level": "info", "site": c.name, "product": p.title, "value": d,
                               "message": f"{c.name} is running {d:.0f}% off on {p.title}"})
    order = {"warning": 0, "risk": 1, "opportunity": 2, "info": 3}
    alerts.sort(key=lambda a: (order[a["level"]], -abs(a.get("value", 0))))
    return alerts


def build_insights(stats: dict[str, dict], per_comp: dict[str, dict], rows: list[dict], source: SiteData, failed: list[SiteData]) -> list[dict]:
    ins: list[dict] = []
    ranked = [c for c in per_comp.values() if c["matched"]]
    total_matched = sum(c["matched"] for c in ranked)
    if ranked:
        avg_gap = statistics.fmean([c["avg_gap_pct"] for c in ranked])
        direction = "lower" if avg_gap > 0 else "higher"
        ins.append({"title": "Overall price position", "severity": "info",
                    "text": f"Across {total_matched} matched product comparisons, your prices are on average {abs(avg_gap):.1f}% {direction} than your competitors'."})
        aggressive = min(ranked, key=lambda c: c["price_index"])
        if aggressive["price_index"] < 100:
            ins.append({"title": "Most aggressive competitor", "severity": "warning",
                        "text": f"{aggressive['name']} is the biggest price threat: {aggressive['cheaper']} of {aggressive['matched']} matched products are cheaper there (average price index {aggressive['price_index']:.0f} vs your 100)."})
        premium = max(ranked, key=lambda c: c["price_index"])
        if premium["price_index"] > 100:
            ins.append({"title": "Premium-priced competitor", "severity": "info",
                        "text": f"{premium['name']} prices {premium['price_index'] - 100:.1f}% above you on average, so there is room to win price-sensitive shoppers from them."})
        cheapest_wins = sum(1 for r in rows if r["source_rank"] == 1)
        ins.append({"title": "Where you lead", "severity": "positive",
                    "text": f"You have the lowest price on {cheapest_wins} of {len(rows)} products that at least one competitor also sells."})
    sale = sorted((s for s in stats.values() if s["on_sale_pct"] is not None), key=lambda s: -s["on_sale_pct"])
    if sale and sale[0]["on_sale_pct"] > 0:
        top = sale[0]
        ins.append({"title": "Discount intensity", "severity": "info",
                    "text": f"{top['name']} has the highest share of products on sale ({top['on_sale_pct']:.0f}%, average discount {top['avg_discount_pct'] or 0:.0f}%)."
                            + (f" You discount {stats[source.key]['on_sale_pct']:.0f}% of your catalogue." if top["key"] != source.key else "")})
    opp = sum(1 for r in rows for v in r["competitors"].values() if v["in_stock"] is False and r["source"]["in_stock"] is True)
    if opp:
        ins.append({"title": "Stock-out opportunities", "severity": "positive",
                    "text": f"Competitors are out of stock on {opp} product listing(s) that you have in stock. These are good candidates for promotion."})
    if failed:
        ins.append({"title": "Incomplete data", "severity": "warning",
                    "text": "Some sites could not be read: " + ", ".join(f.name for f in failed) + ". Results exclude them. See Data quality below."})
    if not ranked:
        ins.append({"title": "No comparable products found", "severity": "warning",
                    "text": "No competitor products could be matched to yours with enough confidence. Check the 'Needs review' list and the data quality section."})
    return ins


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def radar_data(stats: dict[str, dict], rows: list[dict], source_key: str, comp_keys: list[str]) -> dict:
    """0-100 scores per site on up to 5 dimensions. A dimension is dropped (and reported) when any plotted
    site lacks the data, instead of inventing a value."""
    ratios: dict[str, list[float]] = {}
    for r in rows:
        prices = {source_key: r["source"]["price"], **{k: v["price"] for k, v in r["competitors"].items()}}
        mean = statistics.fmean(prices.values())
        for k, v in prices.items():
            ratios.setdefault(k, []).append(v / mean)
    keys = [k for k in [source_key, *comp_keys] if k in ratios and k in stats]
    max_count = max((stats[k]["product_count"] for k in keys), default=0) or 1
    dims: dict[str, dict[str, float | None]] = {
        "Price competitiveness": {k: _clamp(100 - (statistics.fmean(ratios[k]) - 0.9) * 500) for k in keys},
        "Discount intensity": {k: stats[k]["on_sale_pct"] for k in keys},
        "Stock availability": {k: (stats[k]["in_stock"] / (stats[k]["in_stock"] + stats[k]["out_of_stock"]) * 100
                                   if stats[k]["in_stock"] + stats[k]["out_of_stock"] else None) for k in keys},
        "Catalogue size": {k: stats[k]["product_count"] / max_count * 100 for k in keys},
        "Customer rating": {k: (stats[k]["avg_rating"] / 5 * 100 if stats[k]["avg_rating"] else None) for k in keys},
    }
    axes, omitted = [], {}
    for name, vals in dims.items():
        missing = [stats[k]["name"] for k in keys if vals[k] is None]
        if missing:
            omitted[name] = "no data for " + ", ".join(missing)
        else:
            axes.append(name)
    return {"axes": axes, "omitted": omitted,
            "series": {k: [round(dims[a][k], 1) for a in axes] for k in keys} if len(axes) >= 3 else {}}


def build_recommendations(alerts: list[dict], rows: list[dict], stats: dict[str, dict], per_comp: dict[str, dict],
                          source: SiteData, comps: list[SiteData], review_count: int, undercut_pct: float) -> list[dict]:
    recs: list[dict] = []
    top = lambda items, n=3: "; ".join(items[:n])  # noqa: E731
    by_site: dict[str, list[dict]] = {}
    for a in alerts:
        if a["type"] == "undercut":
            by_site.setdefault(a["site"], []).append(a)
    for site, items in sorted(by_site.items(), key=lambda kv: -len(kv[1])):
        worst = max(abs(a["value"]) for a in items)
        recs.append({"priority": "high" if len(items) >= 3 or worst >= 15 else "medium",
                     "action": f"Review pricing on {len(items)} product(s) where {site} undercuts you by {undercut_pct:.0f}% or more",
                     "detail": f"Largest gap {worst:.0f}%. Examples: " + top([f"{a['product']} ({a['value']:+.0f}%)" for a in items])})
    oos = [a for a in alerts if a["type"] == "competitor_out_of_stock"]
    if oos:
        recs.append({"priority": "medium", "action": f"Promote {len(oos)} product(s) while competitors are out of stock",
                     "detail": "Examples: " + top([f"{a['product']} ({a['site']})" for a in oos])})
    mine_oos = [a for a in alerts if a["type"] == "you_out_of_stock"]
    if mine_oos:
        recs.append({"priority": "high", "action": f"Restock or relist {len(mine_oos)} product(s) that competitors are selling while you are out of stock",
                     "detail": "Examples: " + top([a["product"] for a in mine_oos])})
    need = min(2, len(comps))
    headroom = [r for r in rows if len(r["competitors"]) >= need and all(v["gap_pct"] >= 10 for v in r["competitors"].values())]
    if headroom:
        recs.append({"priority": "low", "action": f"Test small price increases on {len(headroom)} product(s) where you are 10% or more cheaper than every competitor",
                     "detail": "Check margins first. Examples: " + top([r["source"]["title"] for r in headroom])})
    mine = stats[source.key]["on_sale_pct"] or 0
    heavy = [s for k, s in stats.items() if k != source.key and (s["on_sale_pct"] or 0) >= 20 and (s["on_sale_pct"] or 0) >= 1.5 * mine]
    for s in heavy[:2]:
        recs.append({"priority": "medium", "action": f"Consider targeted promotions: {s['name']} discounts {s['on_sale_pct']:.0f}% of its catalogue vs your {mine:.0f}%",
                     "detail": f"Their average discount is {s['avg_discount_pct'] or 0:.0f}%."})
    if review_count:
        recs.append({"priority": "low", "action": f"Check {review_count} uncertain product match(es)",
                     "detail": "They were left out of the comparison. Confirm them to improve coverage."})
    failed = [c.name for c in comps if c.status != "ok"]
    if failed:
        recs.append({"priority": "medium", "action": "Fix data access for: " + ", ".join(failed),
                     "detail": "These sites could not be read, so the comparison excludes them."})
    order = {"high": 0, "medium": 1, "low": 2}
    recs.sort(key=lambda r: order[r["priority"]])
    return recs


def _shipping(sd: SiteData) -> int | None:
    sig = sd.signals or {}
    return 0 if sig.get("free_shipping_all") else sig.get("free_shipping_threshold")


def signal_insights(sites: list[SiteData]) -> list[dict]:
    ins: list[dict] = []
    src = next(s for s in sites if s.is_source)
    known = {sd.name: _shipping(sd) for sd in sites if sd.signals and _shipping(sd) is not None}
    if known:
        mine = known.get(src.name)
        parts = [f"{n}: " + ("free on all orders" if v == 0 else f"free above ₹{v:,}") for n, v in known.items()]
        better = [n for n, v in known.items() if n != src.name and (mine is None or v < mine)]
        text = "Free-shipping offers found on homepages or policy pages. " + "; ".join(parts) + "."
        if better and mine is not None:
            text += f" {', '.join(better)} set a lower threshold than you."
        elif better:
            text += " No free-shipping offer was found for your site."
        ins.append({"title": "Free shipping", "severity": "warning" if better else "info", "text": text})
    coupons = [(sd.name, o["code"], o.get("discount_value")) for sd in sites if sd.signals and not sd.is_source for o in sd.signals["offers"] if o.get("code")]
    if coupons:
        ins.append({"title": "Coupon codes", "severity": "info",
                    "text": "Competitors advertise these codes: " + "; ".join(f"{n} {c}" + (f" ({d:.0f}% off)" if d else "") for n, c, d in coupons[:5]) + "."})
    ads = [sd.name for sd in sites if sd.signals and not sd.is_source and sd.signals["marketing_stack"].get("advertising")]
    if ads:
        ins.append({"title": "Ad tracking found", "severity": "info",
                    "text": "Advertising pixels or tags were found on " + ", ".join(ads) + ". That suggests paid campaigns or retargeting, but it does not prove ads are live. Check the ad library links below."})
    windows = {sd.name: sd.signals["policies"]["return_window_days"] for sd in sites if sd.signals and (sd.signals.get("policies") or {}).get("return_window_days")}
    if windows:
        ins.append({"title": "Return windows", "severity": "info", "text": "Return windows found: " + "; ".join(f"{n} {d} days" for n, d in windows.items()) + "."})
    return ins


def social_insights(sites: list[SiteData]) -> list[dict]:
    rows = [(sd.name, (sd.social or {}).get("youtube")) for sd in sites]
    ok = [(n, y) for n, y in rows if y and y.get("status") == "ok" and y.get("followers")]
    if len(ok) < 1:
        return []
    top = max(ok, key=lambda t: t[1]["followers"])
    text = f"{top[0]} has the largest YouTube channel found ({top[1]['followers']:,} subscribers"
    if top[1].get("posts_last_30d") is not None:
        text += f", {top[1]['posts_last_30d']} video(s) in the last 30 days"
    text += "). " + "Other channels: " + "; ".join(f"{n} {y['followers']:,}" for n, y in ok if n != top[0]) + "." if len(ok) > 1 else ")."
    return [{"title": "YouTube presence", "severity": "info", "text": text}]


def signal_recommendations(sites: list[SiteData]) -> list[dict]:
    recs: list[dict] = []
    src = next(s for s in sites if s.is_source)
    mine = _shipping(src)
    better = [(sd.name, _shipping(sd)) for sd in sites if not sd.is_source and sd.signals and _shipping(sd) is not None and (mine is None or _shipping(sd) < mine)]
    if better:
        recs.append({"priority": "medium", "action": "Review your free-shipping threshold",
                     "detail": "; ".join(f"{n}: {'free on all orders' if v == 0 else f'free above ₹{v:,}'}" for n, v in better[:3]) + (f". Yours: free above ₹{mine:,}." if mine else ". No offer found on your site.")})
    my_ret = ((src.signals or {}).get("policies") or {}).get("return_window_days")
    longer = [(sd.name, sd.signals["policies"]["return_window_days"]) for sd in sites if not sd.is_source and sd.signals and (sd.signals.get("policies") or {}).get("return_window_days")
              and my_ret and sd.signals["policies"]["return_window_days"] > my_ret]
    if longer:
        recs.append({"priority": "low", "action": "Consider a longer return window", "detail": "; ".join(f"{n}: {d} days" for n, d in longer[:3]) + f". Yours: {my_ret} days."})
    return recs


def analyze(sites: list[SiteData], *, match_threshold: float = 0.85, undercut_alert_pct: float = 5.0) -> dict:
    source = next(s for s in sites if s.is_source)
    comps = [s for s in sites if not s.is_source]
    ok_comps = [c for c in comps if c.status == "ok"]
    stats = {s.key: site_stats(s) for s in sites if s.status == "ok"}
    result: dict = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source_key": source.key, "currency": "INR", "match_threshold": match_threshold,
        "sites": stats,
        "data_quality": [
            {"key": s.key, "name": s.name, "role": "source" if s.is_source else "competitor", "status": s.status,
             "platform": s.platform, "method": s.method, "products": len(s.products), "error": s.error}
            for s in sites
        ],
        "comparison_available": source.status == "ok" and bool(ok_comps),
        "rows": [], "competitors": [], "alerts": [], "insights": [], "review_matches": [], "coverage": {},
        "signals": {sd.key: sd.signals for sd in sites if sd.signals},
        "social": {sd.key: {"links": (sd.signals or {}).get("social_links", {}), "youtube": (sd.social or {}).get("youtube")} for sd in sites if sd.signals or sd.social},
        "ad_links": {sd.key: ad_library_links(sd.name, ((sd.signals or {}).get("social_links", {}).get("facebook") or {}).get("handle")) for sd in sites},
        "recommendations": [], "price_points": {}, "discount_points": {}, "radar": {"axes": [], "omitted": {}, "series": {}},
    }
    if not result["comparison_available"]:
        return result

    matches = {c.key: match_products(source.products, c.products, match_threshold) for c in ok_comps}
    rows, per_comp = compare(source, ok_comps, matches)
    failed = [c for c in comps if c.status != "ok"]
    result["rows"] = rows
    result["competitors"] = list(per_comp.values())
    result["alerts"] = build_alerts(source, ok_comps, rows, undercut_alert_pct)
    result["insights"] = build_insights(stats, per_comp, rows, source, failed) + signal_insights(sites) + social_insights(sites)
    result["price_points"] = {k: sorted(p.price for p in sd.products if p.price)[:500] for sd in sites if sd.status == "ok" for k in [sd.key]}
    result["discount_points"] = {sd.key: [d for p in sd.products if (d := discount_pct(p)) is not None] for sd in sites if sd.status == "ok"}
    result["radar"] = radar_data(stats, rows, source.key, [c.key for c in ok_comps])
    for c in ok_comps:
        matched_src = {m.source_idx for m in matches[c.key] if m.status == "matched"}
        matched_cmp = {m.comp_idx for m in matches[c.key] if m.status == "matched"}
        result["coverage"][c.key] = {
            "name": c.name, "source_products_matched": len(matched_src), "source_products": len(source.products),
            "competitor_products_unmatched": len(c.products) - len(matched_cmp),
        }
        for m in matches[c.key]:
            if m.status == "review":
                result["review_matches"].append({
                    "competitor": c.name, "confidence": m.confidence, "method": m.method,
                    "source_title": source.products[m.source_idx].title, "competitor_title": c.products[m.comp_idx].title,
                    "source_price": source.products[m.source_idx].price, "competitor_price": c.products[m.comp_idx].price,
                })
    result["review_matches"].sort(key=lambda r: -r["confidence"])
    result["recommendations"] = build_recommendations(result["alerts"], rows, stats, per_comp, source, comps, len(result["review_matches"]), undercut_alert_pct)
    result["recommendations"] += signal_recommendations(sites)
    result["recommendations"].sort(key=lambda r: {"high": 0, "medium": 1, "low": 2}[r["priority"]])
    return result
