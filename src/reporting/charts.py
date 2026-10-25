"""Report charts (matplotlib + seaborn). Thread-safe: object-oriented Figure API under a lock, never pyplot."""
from __future__ import annotations

import math
import statistics
import struct
import textwrap
import threading
from dataclasses import dataclass
from io import BytesIO

import numpy as np
import pandas as pd
import seaborn as sns
from matplotlib import rc_context
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

SOURCE_COLOR = "#2563eb"
OKABE = ["#E69F00", "#56B4E9", "#009E73", "#CC79A7", "#D55E00", "#8c8c8c"]  # colour-blind friendly
THREAT, SAFE, NEUTRAL, GOOD = "#D55E00", "#0072B2", "#9aa5b8", "#009E73"
GAP_CMAP = LinearSegmentedColormap.from_list("gap", [THREAT, "#f7f7f7", SAFE])
RC = {
    "font.family": "DejaVu Sans", "font.size": 9, "axes.titlesize": 11, "axes.titleweight": "bold", "axes.titlelocation": "left",
    "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "grid.color": "#e6e9ef", "grid.linewidth": 0.6,
    "axes.axisbelow": True, "axes.edgecolor": "#9aa5b8", "legend.frameon": False, "figure.facecolor": "white",
}
_LOCK = threading.Lock()
inr = FuncFormatter(lambda x, _: f"₹{x:,.0f}")


@dataclass
class ChartOut:
    png: bytes
    aspect: float  # height / width
    takeaway: str


def short(text: str, n: int = 24) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _finish(fig: Figure, takeaway: str) -> ChartOut:
    FigureCanvasAgg(fig)
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=200, bbox_inches="tight", facecolor="white")
    png = buf.getvalue()
    w, h = struct.unpack(">II", png[16:24])
    return ChartOut(png, h / w, takeaway)


def palette(result: dict) -> dict[str, str]:
    pal = {result["source_key"]: SOURCE_COLOR}
    for i, c in enumerate(result["competitors"]):
        pal[c["key"]] = OKABE[i % len(OKABE)]
    return pal


def _names(result: dict) -> dict[str, str]:
    return {k: s["name"] for k, s in result["sites"].items()}


def _order(result: dict) -> list[str]:
    return [result["source_key"], *[c["key"] for c in result["competitors"]]]


def _gap_rows(result: dict) -> list[dict]:
    return result["rows"]


# ---------------------------------------------------------------- price charts
def chart_price_bars(result: dict) -> ChartOut | None:
    rows = sorted(_gap_rows(result), key=lambda r: (-len(r["competitors"]), -(r["source"]["price"] or 0)))[:8]
    if not rows:
        return None
    names, pal = _names(result), palette(result)
    keys = _order(result)
    recs = []
    for r in rows:
        label = "\n".join(textwrap.wrap(r["source"]["title"], 15)[:3])
        recs.append({"product": label, "site": names[result["source_key"]], "price": r["source"]["price"]})
        for k, v in r["competitors"].items():
            recs.append({"product": label, "site": names[k], "price": v["price"]})
    df = pd.DataFrame(recs)
    fig = Figure(figsize=(7.4, 3.9))
    ax = fig.subplots()
    hue_order = [names[k] for k in keys if names[k] in set(df["site"])]
    sns.barplot(data=df, x="product", y="price", hue="site", hue_order=hue_order, palette={names[k]: pal[k] for k in keys},
                ax=ax, saturation=1, edgecolor="white", linewidth=0.4)
    ax.yaxis.set_major_formatter(inr)
    ax.set_xlabel("")
    ax.set_ylabel("Price (₹)")
    ax.set_title("Price by product: you vs competitors")
    ax.tick_params(axis="x", labelsize=6.6)
    ax.grid(axis="x", visible=False)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.24), ncol=min(len(hue_order), 3), fontsize=8)
    all_rows = _gap_rows(result)
    wins = sum(1 for r in all_rows if r["source_rank"] == 1)
    cheapest = {}
    for r in all_rows:
        if r["cheapest"]:
            cheapest[r["cheapest"]] = cheapest.get(r["cheapest"], 0) + 1
    other = max(cheapest.items(), key=lambda kv: kv[1]) if cheapest else None
    take = f"You have the lowest price on {wins} of {len(all_rows)} matched products."
    if other:
        take += f" {names[other[0]]} is cheapest most often ({other[1]} products)."
    return _finish(fig, take)


def chart_gap_heatmap(result: dict) -> ChartOut | None:
    comps = [c for c in result["competitors"] if c["matched"]]
    rows = sorted(_gap_rows(result), key=lambda r: -max(abs(v["gap_pct"]) for v in r["competitors"].values()))[:25]
    if not rows or not comps:
        return None
    data = pd.DataFrame(
        {c["name"]: [r["competitors"].get(c["key"], {}).get("gap_pct", np.nan) for r in rows] for c in comps},
        index=[short(r["source"]["title"], 34) for r in rows],
    )
    ann = data.map(lambda v: "" if pd.isna(v) else f"{v:+.0f}%")
    m = float(min(40, max(10, np.nanmax(np.abs(data.to_numpy())))))
    fig = Figure(figsize=(7.4, max(2.6, 0.27 * len(rows) + 1.3)))
    ax = fig.subplots()
    sns.heatmap(data, annot=ann, fmt="", cmap=GAP_CMAP, vmin=-m, vmax=m, center=0, linewidths=0.5, linecolor="white", ax=ax,
                annot_kws={"fontsize": 7}, cbar_kws={"label": "Competitor price vs yours (%)", "shrink": 0.7}, mask=data.isna())
    ax.set_title("Price gap by product (competitor vs you)")
    ax.set_xlabel("")
    ax.set_ylabel("")
    ax.tick_params(axis="x", labelsize=8, rotation=0)
    ax.tick_params(axis="y", labelsize=7)
    ax.grid(False)
    cheaper = int((data <= -5).sum().sum())
    return _finish(fig, f"Orange cells mean the competitor is cheaper than you; blue means dearer. {cheaper} cell(s) show a competitor at least 5% cheaper.")


def chart_position(result: dict) -> ChartOut | None:
    comps = [c for c in result["competitors"] if c["matched"]]
    if not comps:
        return None
    fig = Figure(figsize=(7.4, max(2.2, 0.55 * len(comps) + 1.2)))
    ax = fig.subplots()
    labels = [short(c["name"], 26) for c in comps]
    left = np.zeros(len(comps))
    for field, label, color in (("cheaper", "Cheaper than you", THREAT), ("same", "About the same (±1%)", NEUTRAL), ("costlier", "Costlier than you", SAFE)):
        vals = np.array([c[field] for c in comps], dtype=float)
        bars = ax.barh(labels, vals, left=left, color=color, label=label, edgecolor="white", height=0.6)
        for bar, v, l0 in zip(bars, vals, left, strict=True):
            if v >= 1:
                ax.text(l0 + v / 2, bar.get_y() + bar.get_height() / 2, f"{int(v)}", ha="center", va="center", color="white", fontsize=8, fontweight="bold")
        left += vals
    ax.invert_yaxis()
    ax.set_xlabel("Matched products")
    ax.set_title("How each competitor prices against you")
    ax.grid(axis="y", visible=False)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=3, fontsize=8)
    top = min(comps, key=lambda c: c["price_index"])
    take = (f"{top['name']} is the most aggressive: cheaper on {top['cheaper']} of {top['matched']} matched products (price index {top['price_index']:.0f}, you = 100)."
            if top["price_index"] < 100 else "No competitor is cheaper than you on average.")
    return _finish(fig, take)


def chart_distribution(result: dict) -> ChartOut | None:
    names, pal = _names(result), palette(result)
    recs = [{"site": names[k], "price": p} for k in _order(result) for p in result["price_points"].get(k, [])]
    if len({r["site"] for r in recs}) < 2:
        return None
    df = pd.DataFrame(recs)
    order = [names[k] for k in _order(result) if names[k] in set(df["site"])]
    fig = Figure(figsize=(7.4, max(2.4, 0.6 * len(order) + 1.2)))
    ax = fig.subplots()
    colors = {names[k]: pal[k] for k in _order(result) if k in pal}
    sns.boxplot(data=df, x="price", y="site", hue="site", order=order, hue_order=order, palette=colors, legend=False, ax=ax,
                width=0.55, fliersize=0, linewidth=0.9, saturation=1)
    sns.stripplot(data=df, x="price", y="site", order=order, color="#1c2230", alpha=0.35, size=2.2, ax=ax, jitter=0.2)
    if df["price"].max() / max(df["price"].min(), 1) > 15:
        ax.set_xscale("log")
        ax.set_xlabel("Price (₹, log scale)")
    else:
        ax.set_xlabel("Price (₹)")
    ax.xaxis.set_major_formatter(inr)
    ax.set_ylabel("")
    ax.set_title("Price distribution across each catalogue")
    ax.set_yticks(range(len(order)), [short(o, 26) for o in order])
    ax.grid(axis="y", visible=False)
    med = {names[k]: statistics.median(result["price_points"][k]) for k in _order(result) if result["price_points"].get(k)}
    take = "Median price: " + "; ".join(f"{short(n, 20)} ₹{v:,.0f}" for n, v in med.items()) + ". Boxes show the middle 50% of products."
    return _finish(fig, take)


# ------------------------------------------------------- discounts, stock, etc.
def chart_discounts(result: dict) -> ChartOut | None:
    names, pal = _names(result), palette(result)
    keys = [k for k in _order(result) if k in result["sites"]]
    fig = Figure(figsize=(7.4, 3.3))
    ax1, ax2 = fig.subplots(1, 2, gridspec_kw={"width_ratios": [1, 1.2]})
    vals = [result["sites"][k]["on_sale_pct"] or 0 for k in keys]
    labels = [short(names[k], 18) for k in keys]
    ax1.barh(labels, vals, color=[pal.get(k, NEUTRAL) for k in keys], edgecolor="white")
    for y, v in enumerate(vals):
        ax1.text(v + 0.8, y, f"{v:.0f}%", va="center", fontsize=8)
    ax1.invert_yaxis()
    ax1.set_xlim(0, max(max(vals) * 1.25, 10))
    ax1.set_title("Products on sale (%)")
    ax1.grid(axis="y", visible=False)
    recs = [{"site": names[k], "d": d} for k in keys for d in result["discount_points"].get(k, [])]
    if recs:
        df = pd.DataFrame(recs)
        order = [names[k] for k in keys if names[k] in set(df["site"])]
        sns.boxplot(data=df, x="d", y="site", hue="site", order=order, hue_order=order, palette={names[k]: pal.get(k, NEUTRAL) for k in keys},
                    legend=False, ax=ax2, width=0.5, fliersize=0, linewidth=0.9, saturation=1)
        sns.stripplot(data=df, x="d", y="site", order=order, color="#1c2230", alpha=0.4, size=2.5, ax=ax2, jitter=0.2)
        ax2.set_yticks(range(len(order)), [short(o, 18) for o in order])
        ax2.set_xlabel("Discount depth (%)")
        ax2.set_ylabel("")
        ax2.grid(axis="y", visible=False)
    else:
        ax2.text(0.5, 0.5, "No discounts detected", ha="center", va="center", transform=ax2.transAxes, color="#5d6678")
        ax2.set_axis_off()
    ax2.set_title("How deep the discounts go")
    fig.subplots_adjust(wspace=0.55)
    top = max(keys, key=lambda k: result["sites"][k]["on_sale_pct"] or 0)
    s = result["sites"][top]
    take = (f"{s['name']} discounts the largest share of its catalogue ({s['on_sale_pct']:.0f}%, average depth {s['avg_discount_pct'] or 0:.0f}%)."
            if (s["on_sale_pct"] or 0) > 0 else "No site shows list-price discounts in the data we could read.")
    return _finish(fig, take)


def chart_stock(result: dict) -> ChartOut | None:
    names = _names(result)
    keys = [k for k in _order(result) if k in result["sites"]]
    fig = Figure(figsize=(7.4, max(2.3, 0.55 * len(keys) + 1.2)))
    ax = fig.subplots()
    labels = [short(names[k], 22) + (f"  ({result['sites'][k]['low_stock']} low)" if result["sites"][k]["low_stock"] else "") for k in keys]
    left = np.zeros(len(keys))
    for field, label, color in (("in_stock", "In stock", GOOD), ("out_of_stock", "Out of stock", THREAT), ("unknown_stock", "Unknown (not published)", NEUTRAL)):
        vals = np.array([result["sites"][k][field] for k in keys], dtype=float)
        bars = ax.barh(labels, vals, left=left, color=color, label=label, edgecolor="white", height=0.6)
        for bar, v, l0 in zip(bars, vals, left, strict=True):
            if v >= 1:
                ax.text(l0 + v / 2, bar.get_y() + bar.get_height() / 2, f"{int(v)}", ha="center", va="center", color="white", fontsize=8, fontweight="bold")
        left += vals
    ax.invert_yaxis()
    ax.set_xlabel("Products")
    ax.set_title("Stock status by site")
    ax.grid(axis="y", visible=False)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=3, fontsize=8)
    unknown = [names[k] for k in keys if result["sites"][k]["unknown_stock"] == result["sites"][k]["product_count"]]
    take = ("Stock is not published by: " + ", ".join(unknown) + ". We report it as unknown rather than guess." if unknown
            else "Numbers in brackets show products flagged low stock by the site.")
    return _finish(fig, take)


def chart_coverage(result: dict) -> ChartOut | None:
    cov = result.get("coverage") or {}
    if not cov:
        return None
    labels = [short(c["name"], 18) for c in cov.values()]
    both = [c["source_products_matched"] for c in cov.values()]
    only_you = [c["source_products"] - c["source_products_matched"] for c in cov.values()]
    only_them = [c["competitor_products_unmatched"] for c in cov.values()]
    x = np.arange(len(labels))
    fig = Figure(figsize=(7.4, 3.3))
    ax = fig.subplots()
    for i, (vals, label, color) in enumerate(((both, "Sold by both", SAFE), (only_you, "Only you sell (no match found)", SOURCE_COLOR), (only_them, "Only they sell (no match found)", OKABE[0]))):
        bars = ax.bar(x + (i - 1) * 0.26, vals, 0.26, label=label, color=color, edgecolor="white")
        ax.bar_label(bars, fontsize=7, padding=1)
    ax.set_xticks(x, labels, fontsize=8)
    ax.set_ylabel("Products")
    ax.set_title("Catalogue overlap with each competitor")
    ax.grid(axis="x", visible=False)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.15), ncol=3, fontsize=8)
    worst = min(cov.values(), key=lambda c: c["source_products_matched"])
    return _finish(fig, f"Lowest overlap: {worst['name']} (matched {worst['source_products_matched']} of your {worst['source_products']} products). "
                        "Unmatched products may be genuinely different or just named differently.")


def chart_ratings(result: dict) -> ChartOut | None:
    names, pal = _names(result), palette(result)
    keys = [k for k in _order(result) if (result["sites"].get(k) or {}).get("avg_rating")]
    if len(keys) < 2:
        return None
    fig = Figure(figsize=(7.4, 2.8))
    ax = fig.subplots()
    vals = [result["sites"][k]["avg_rating"] for k in keys]
    bars = ax.bar([short(names[k], 18) for k in keys], vals, color=[pal[k] for k in keys], edgecolor="white", width=0.55)
    for bar, k in zip(bars, keys, strict=True):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.05, f"{bar.get_height():.1f}\n({result['sites'][k]['rated_count']} rated)", ha="center", fontsize=8)
    ax.set_ylim(0, 5.8)
    ax.set_ylabel("Average rating (out of 5)")
    ax.set_title("Customer ratings")
    ax.grid(axis="x", visible=False)
    return _finish(fig, "Only products that publish ratings are counted. Sites without ratings are left out.")


def chart_radar(result: dict) -> ChartOut | None:
    rd = result.get("radar") or {}
    if not rd.get("series"):
        return None
    names, pal = _names(result), palette(result)
    axes_names = rd["axes"]
    angles = np.linspace(0, 2 * np.pi, len(axes_names), endpoint=False).tolist()
    fig = Figure(figsize=(6.2, 4.6))
    ax = fig.add_subplot(projection="polar")
    for k, vals in rd["series"].items():
        v = [*vals, vals[0]]
        a = [*angles, angles[0]]
        is_src = k == result["source_key"]
        ax.plot(a, v, color=pal[k], linewidth=2.6 if is_src else 1.4, label=short(names[k], 24) + (" (you)" if is_src else ""))
        if is_src:
            ax.fill(a, v, color=pal[k], alpha=0.15)
    ax.set_xticks(angles, [textwrap.fill(n, 16) for n in axes_names], fontsize=8)
    ax.tick_params(axis="x", pad=14)
    ax.set_ylim(0, 100)
    ax.set_yticks([25, 50, 75, 100], ["25", "50", "75", "100"], fontsize=6, color="#5d6678")
    ax.set_title("Competitive positioning (0-100, higher is better)", pad=18)
    ax.legend(loc="upper left", bbox_to_anchor=(1.05, 1.05), fontsize=8)
    note = "Price: 50 = market average, higher = cheaper. "
    if rd.get("omitted"):
        note += "Left out for lack of data: " + "; ".join(f"{k} ({v})" for k, v in rd["omitted"].items()) + "."
    return _finish(fig, note)


def chart_price_history(result: dict) -> ChartOut | None:
    ph = result.get("price_history")
    if not ph or len(ph.get("labels", [])) < 2 or not ph.get("products"):
        return None
    names, pal = _names(result), palette(result)
    prods = ph["products"][:6]
    cols = 2 if len(prods) > 1 else 1
    rows = math.ceil(len(prods) / cols)
    fig = Figure(figsize=(7.4, 2.25 * rows + 0.7))
    axes = fig.subplots(rows, cols, squeeze=False)
    x = list(range(len(ph["labels"])))
    handles = {}
    moves = []
    for ax, prod in zip(axes.flat, prods, strict=False):
        for key, vals in prod["series"].items():
            ys = [np.nan if v is None else v for v in vals]
            line, = ax.plot(x, ys, marker="o", ms=3, lw=2.6 if key == result["source_key"] else 1.4, color=pal.get(key, NEUTRAL))
            handles[names.get(key, key)] = line
            seen = [v for v in vals if v]
            if len(seen) >= 2 and seen[0]:
                moves.append(((seen[-1] - seen[0]) / seen[0] * 100, names.get(key, key), prod["title"]))
        ax.set_title(short(prod["title"], 36), fontsize=8, fontweight="bold")
        ax.yaxis.set_major_formatter(inr)
        ax.set_xticks(x, ph["labels"], rotation=30, ha="right", fontsize=6)
        ax.tick_params(axis="y", labelsize=6.5)
    for ax in list(axes.flat)[len(prods):]:
        ax.set_axis_off()
    fig.legend(list(handles.values()), list(handles), loc="lower center", ncol=min(len(handles), 4), fontsize=8)
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    big = max(moves, key=lambda m: abs(m[0]), default=None)
    take = ("No price moved across the analyses shown." if not big or abs(big[0]) < 0.5
            else f"Biggest move: {big[1]} changed {short(big[2], 32)} by {big[0]:+.1f}% across the period shown.")
    return _finish(fig, take)


def chart_change_frequency(result: dict) -> ChartOut | None:
    cf = result.get("change_frequency") or {}
    if len(cf) < 2:
        return None
    names, pal = _names(result), palette(result)
    keys = [k for k in _order(result) if k in cf]
    fig = Figure(figsize=(7.4, max(2.2, 0.5 * len(keys) + 1.1)))
    ax = fig.subplots()
    vals = [cf[k]["pct"] for k in keys]
    ax.barh([short(names[k], 24) for k in keys], vals, color=[pal.get(k, NEUTRAL) for k in keys], edgecolor="white", height=0.6)
    for y, k in enumerate(keys):
        ax.text(vals[y] + 0.5, y, f"{cf[k]['changes']} of {cf[k]['observations']} price checks", va="center", fontsize=7.5)
    ax.invert_yaxis()
    ax.set_xlim(0, max(max(vals) * 1.6, 10))
    ax.set_xlabel("Share of price checks where the price changed (%)")
    ax.set_title("How often each site changes prices")
    ax.grid(axis="y", visible=False)
    top = max(keys, key=lambda k: cf[k]["pct"])
    return _finish(fig, f"{names[top]} reprices most often ({cf[top]['pct']:.0f}% of checks). Based on the analyses stored so far.")


def chart_social(result: dict) -> ChartOut | None:
    names, pal = _names(result), palette(result)
    rows = [(k, (v or {}).get("youtube") or {}) for k, v in (result.get("social") or {}).items()]
    rows = [(k, y) for k, y in rows if y.get("status") == "ok" and y.get("followers")]
    if len(rows) < 2:
        return None
    fig = Figure(figsize=(7.4, max(2.2, 0.5 * len(rows) + 1.1)))
    ax1, ax2 = fig.subplots(1, 2)
    labels = [short(names[k], 18) for k, _ in rows]
    ax1.barh(labels, [y["followers"] for _, y in rows], color=[pal.get(k, NEUTRAL) for k, _ in rows], edgecolor="white")
    ax1.set_title("YouTube subscribers")
    ax1.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax2.barh(labels, [y.get("posts_last_30d") or 0 for _, y in rows], color=[pal.get(k, NEUTRAL) for k, _ in rows], edgecolor="white")
    ax2.set_title("Videos in the last 30 days")
    ax2.set_yticklabels([])
    for ax in (ax1, ax2):
        ax.invert_yaxis()
        ax.grid(axis="y", visible=False)
    top = max(rows, key=lambda r: r[1]["followers"])
    return _finish(fig, f"{names[top[0]]} has the largest channel found ({top[1]['followers']:,} subscribers). Only channels linked from each homepage are included.")


CHART_FUNCS = {
    "price_bars": chart_price_bars, "gap_heatmap": chart_gap_heatmap, "position": chart_position, "distribution": chart_distribution,
    "discounts": chart_discounts, "stock": chart_stock, "coverage": chart_coverage, "ratings": chart_ratings, "radar": chart_radar,
    "price_history": chart_price_history, "change_frequency": chart_change_frequency, "social": chart_social,
}


def build_charts(result: dict) -> dict[str, ChartOut]:
    """Render every chart that has enough data. A chart that fails is skipped, never fatal to the report."""
    out: dict[str, ChartOut] = {}
    with _LOCK, rc_context(RC):
        for name, fn in CHART_FUNCS.items():
            try:
                chart = fn(result)
            except Exception as exc:  # pragma: no cover - defensive: one bad chart must not kill the PDF
                from loguru import logger

                logger.exception("chart {} failed: {}", name, exc)
                continue
            if chart is not None:
                out[name] = chart
    return out
