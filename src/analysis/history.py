"""Changes since the previous analysis and price history, computed from the stored snapshots."""
from __future__ import annotations

import re

from sqlalchemy import Engine, func, select

from src.db.models import Offer, PriceSnapshot, Product, Run, Site
from src.db.session import session_scope

MIN_CHANGE_PCT = 0.5
HISTORY_RUNS = 12


def _norm(t: str) -> str:
    return re.sub(r"\W+", " ", t.lower()).strip()


def _snap(s, site_id: int, run_id: int) -> dict[str, dict]:
    rows = s.execute(select(Product.external_id, Product.title, Product.url, PriceSnapshot.price, PriceSnapshot.in_stock)
                     .join(PriceSnapshot, PriceSnapshot.product_id == Product.id)
                     .where(Product.site_id == site_id, PriceSnapshot.run_id == run_id)).all()
    return {r[0]: {"title": r[1], "url": r[2], "price": r[3], "in_stock": r[4]} for r in rows}


def _offers(s, site_id: int, run_id: int) -> dict[str, str]:
    return {_norm(t): t for (t,) in s.execute(select(Offer.text).where(Offer.site_id == site_id, Offer.run_id == run_id)) if t}


def compute_changes(engine: Engine, run_id: int, keys: list[str], cap: int) -> dict:
    out: dict = {"has_history": False, "previous_run_at": None, "sites": {}}
    with session_scope(engine) as s:
        for key in keys:
            site = s.scalar(select(Site).where(Site.key == key))
            if site is None:
                continue
            prev = s.scalar(select(func.max(PriceSnapshot.run_id)).join(Product, Product.id == PriceSnapshot.product_id)
                            .where(Product.site_id == site.id, PriceSnapshot.run_id < run_id))
            cur = _snap(s, site.id, run_id)
            if prev is None or not cur:
                continue
            old = _snap(s, site.id, prev)
            drops, rises = [], []
            for ext, c in cur.items():
                o = old.get(ext)
                if o and o["price"] and c["price"]:
                    pct = (c["price"] - o["price"]) / o["price"] * 100
                    if abs(pct) >= MIN_CHANGE_PCT:
                        (drops if pct < 0 else rises).append({"title": c["title"], "url": c["url"], "old": o["price"], "new": c["price"], "change_pct": round(pct, 1)})
            drops.sort(key=lambda d: d["change_pct"])
            rises.sort(key=lambda d: -d["change_pct"])
            new = [c["title"] for e, c in cur.items() if e not in old]
            gone = [o["title"] for e, o in old.items() if e not in cur]
            prev_offers, cur_offers = _offers(s, site.id, prev), _offers(s, site.id, run_id)
            started = s.get(Run, prev).started_at.isoformat(timespec="seconds")
            out["sites"][key] = {
                "previous_run_at": started, "new_count": len(new), "removed_count": len(gone), "new_products": new[:8], "removed_products": gone[:8],
                "cap_reached": len(cur) >= cap or len(old) >= cap, "price_drop_count": len(drops), "price_increase_count": len(rises),
                "price_drops": drops[:15], "price_increases": rises[:15],
                "went_out_of_stock": [c["title"] for e, c in cur.items() if e in old and old[e]["in_stock"] is True and c["in_stock"] is False][:15],
                "back_in_stock": [c["title"] for e, c in cur.items() if e in old and old[e]["in_stock"] is False and c["in_stock"] is True][:15],
                "new_offers": [t for k, t in cur_offers.items() if k not in prev_offers][:6],
            }
            out["has_history"] = True
            out["previous_run_at"] = min(filter(None, [out["previous_run_at"], started]))
    return out


def history_alerts(changes: dict, names: dict[str, str], source_key: str, drop_alert_pct: float) -> list[dict]:
    alerts = []
    for key, c in changes.get("sites", {}).items():
        if key == source_key:
            continue
        n = names.get(key, key)
        for d in c["price_drops"]:
            if d["change_pct"] <= -drop_alert_pct:
                alerts.append({"type": "price_drop", "level": "warning" if d["change_pct"] <= -10 else "info", "site": n, "product": d["title"], "value": d["change_pct"],
                               "message": f"{n} cut the price of {d['title']} by {abs(d['change_pct']):.0f}% (₹{d['old']:,.0f} to ₹{d['new']:,.0f}) since the last analysis"})
        for t in c["new_offers"]:
            alerts.append({"type": "new_offer", "level": "info", "site": n, "product": "", "message": f"{n} started a new offer: {t[:120]}"})
        if c["new_count"] >= 3 and not c["cap_reached"]:
            alerts.append({"type": "new_products", "level": "info", "site": n, "product": "", "message": f"{n} added {c['new_count']} new products since the last analysis"})
    return alerts


def price_history(engine: Engine, result: dict, runs: int = HISTORY_RUNS) -> dict | None:
    """Price series across recent analyses for the matched products with the widest competitor coverage."""
    rows = sorted(result["rows"], key=lambda r: (-len(r["competitors"]), -(r["source"]["price"] or 0)))[:6]
    if not rows:
        return None
    src = result["source_key"]
    series_by_product: list[dict] = []
    run_ids: set[int] = set()
    with session_scope(engine) as s:
        for r in rows:
            refs = {src: r["source"]["ext_id"], **{k: v["ext_id"] for k, v in r["competitors"].items()}}
            data: dict[str, dict[int, float]] = {}
            for key, ext in refs.items():
                q = (select(PriceSnapshot.run_id, PriceSnapshot.price).join(Product, Product.id == PriceSnapshot.product_id).join(Site, Site.id == Product.site_id)
                     .where(Site.key == key, Product.external_id == ext).order_by(PriceSnapshot.run_id.desc()).limit(runs))
                data[key] = {rid: price for rid, price in s.execute(q) if price}
                run_ids.update(data[key])
            series_by_product.append({"title": r["source"]["title"], "data": data})
        ordered = sorted(run_ids)[-runs:]
        if len(ordered) < 2:
            return None
        labels = [s.get(Run, rid).started_at.strftime("%d %b %H:%M") for rid in ordered]
    return {"labels": labels, "products": [{"title": p["title"], "series": {k: [d.get(rid) for rid in ordered] for k, d in p["data"].items()}} for p in series_by_product]}


def change_frequency(engine: Engine, keys: list[str], runs: int = HISTORY_RUNS) -> dict:
    out = {}
    with session_scope(engine) as s:
        for key in keys:
            q = (select(Product.id, PriceSnapshot.run_id, PriceSnapshot.price).join(PriceSnapshot, PriceSnapshot.product_id == Product.id).join(Site, Site.id == Product.site_id)
                 .where(Site.key == key, PriceSnapshot.price.is_not(None)).order_by(PriceSnapshot.run_id))
            per: dict[int, list[tuple[int, float]]] = {}
            for pid, rid, price in s.execute(q):
                per.setdefault(pid, []).append((rid, price))
            latest = sorted({rid for v in per.values() for rid, _ in v})[-runs:]
            changes = obs = 0
            for v in per.values():
                v = [(rid, p) for rid, p in v if rid in latest]
                for (_, a), (_, b) in zip(v, v[1:], strict=False):
                    obs += 1
                    changes += abs(b - a) / a * 100 >= MIN_CHANGE_PCT
            if obs:
                out[key] = {"changes": changes, "observations": obs, "pct": round(changes / obs * 100, 1)}
    return out


def attach_history(engine: Engine, result: dict, run_id: int, cap: int, drop_alert_pct: float = 5.0) -> None:
    names = {k: s["name"] for k, s in result["sites"].items()}
    changes = compute_changes(engine, run_id, list(result["sites"]), cap)
    result["changes"] = changes
    result["price_history"] = price_history(engine, result) if changes["has_history"] else None
    result["change_frequency"] = change_frequency(engine, list(result["sites"])) if changes["has_history"] else {}
    result["alerts"] += history_alerts(changes, names, result["source_key"], drop_alert_pct)
    order = {"warning": 0, "risk": 1, "opportunity": 2, "info": 3}
    result["alerts"].sort(key=lambda a: (order[a["level"]], -abs(a.get("value", 0))))
