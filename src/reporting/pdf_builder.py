"""Assemble the PDF report with ReportLab (pure pip; no system libraries needed)."""
from __future__ import annotations

import os
import re
from datetime import datetime
from io import BytesIO
from xml.sax.saxutils import escape

import matplotlib
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    CondPageBreak,
    Frame,
    Image,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

from src.reporting.charts import ChartOut, build_charts

NAVY, BLUE, MUTED, LINE, ZEBRA = colors.HexColor("#14213d"), colors.HexColor("#2563eb"), colors.HexColor("#5d6678"), colors.HexColor("#d9dee8"), colors.HexColor("#f3f5f9")
THREAT, SAFE = colors.HexColor("#D55E00"), colors.HexColor("#0072B2")
PAGE_W, PAGE_H = A4
MARGIN = 18 * mm
CONTENT_W = PAGE_W - 2 * MARGIN
FONT, FONT_B, FONT_I = "DejaVu", "DejaVu-Bold", "DejaVu-Oblique"
_fonts_ready = False


def register_fonts() -> None:
    """Embed DejaVu (ships with matplotlib). ReportLab's built-in fonts lack the rupee sign and render a black box."""
    global _fonts_ready
    if _fonts_ready:
        return
    d = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
    for name, file in ((FONT, "DejaVuSans.ttf"), (FONT_B, "DejaVuSans-Bold.ttf"), (FONT_I, "DejaVuSans-Oblique.ttf"), ("DejaVu-BoldOblique", "DejaVuSans-BoldOblique.ttf")):
        pdfmetrics.registerFont(TTFont(name, os.path.join(d, file)))
    pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=FONT_B, italic=FONT_I, boldItalic="DejaVu-BoldOblique")
    _fonts_ready = True


def _styles() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle("base", fontName=FONT, fontSize=9, leading=13, textColor=colors.HexColor("#1c2230"), alignment=TA_LEFT)
    return {
        "base": base,
        "title": ParagraphStyle("title", parent=base, fontName=FONT_B, fontSize=28, leading=34, textColor=colors.white),
        "subtitle": ParagraphStyle("subtitle", parent=base, fontSize=13, leading=18, textColor=colors.HexColor("#dbe6ff")),
        "H1": ParagraphStyle("H1", parent=base, fontName=FONT_B, fontSize=17, leading=22, textColor=NAVY, spaceBefore=4, spaceAfter=8, keepWithNext=1),
        "H2": ParagraphStyle("H2", parent=base, fontName=FONT_B, fontSize=11.5, leading=15, textColor=NAVY, spaceBefore=10, spaceAfter=4),
        "caption": ParagraphStyle("caption", parent=base, fontName=FONT_I, fontSize=8.3, leading=11.5, textColor=MUTED, spaceBefore=2, spaceAfter=10),
        "small": ParagraphStyle("small", parent=base, fontSize=7.4, leading=9.6),
        "cell": ParagraphStyle("cell", parent=base, fontSize=7.6, leading=9.8),
        "cellb": ParagraphStyle("cellb", parent=base, fontName=FONT_B, fontSize=7.6, leading=9.8, textColor=colors.white),
        "kpi": ParagraphStyle("kpi", parent=base, fontName=FONT_B, fontSize=16, leading=21, textColor=NAVY),
        "kpil": ParagraphStyle("kpil", parent=base, fontSize=7.6, leading=10, textColor=MUTED),
        "bullet": ParagraphStyle("bullet", parent=base, leftIndent=10, bulletIndent=0, spaceAfter=4),
    }


def esc(x) -> str:
    return escape(str(x)) if x is not None else ""


def inr(v) -> str:
    return "–" if v is None else f"₹{v:,.0f}"


def _fmt_date(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d %b %Y, %H:%M UTC") if iso else ""
    except ValueError:
        return iso or ""


class ReportDoc(BaseDocTemplate):
    def __init__(self, buf, title: str, footer_text: str, **kw):
        super().__init__(buf, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN, bottomMargin=MARGIN + 4 * mm,
                         title=title, author="Competitor Price Intelligence", **kw)
        self.footer_text = footer_text
        frame = Frame(MARGIN, MARGIN + 4 * mm, CONTENT_W, PAGE_H - 2 * MARGIN - 4 * mm, id="f", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        self.addPageTemplates([PageTemplate(id="cover", frames=[frame], onPage=self._cover_page), PageTemplate(id="body", frames=[frame], onPage=self._body_page)])

    def _cover_page(self, canv, doc):
        canv.saveState()
        canv.setFillColor(NAVY)
        canv.rect(0, PAGE_H - 120 * mm, PAGE_W, 120 * mm, stroke=0, fill=1)
        canv.setFillColor(BLUE)
        canv.rect(0, PAGE_H - 123 * mm, PAGE_W, 3 * mm, stroke=0, fill=1)
        canv.restoreState()

    def _body_page(self, canv, doc):
        canv.saveState()
        canv.setStrokeColor(LINE)
        canv.line(MARGIN, MARGIN, PAGE_W - MARGIN, MARGIN)
        canv.setFont(FONT, 7.5)
        canv.setFillColor(MUTED)
        canv.drawString(MARGIN, MARGIN - 9, self.footer_text)
        canv.drawRightString(PAGE_W - MARGIN, MARGIN - 9, f"Page {doc.page}")
        canv.restoreState()

    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and flowable.style.name == "H1":
            text = flowable.getPlainText()
            key = "h1-" + "".join(ch for ch in text.lower() if ch.isalnum())  # deterministic: stable across multiBuild passes
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text, key, 0, 0)
            self.notify("TOCEntry", (0, text, self.page, key))


def wrap_name(name: str, width: int = 11, max_lines: int = 3) -> str:
    """Break a domain-like name at '.', ':' or '-' so it fits a narrow column header."""
    lines: list[str] = []
    cur = ""
    for tok in re.split(r"(?<=[.:\-])", name):
        while len(tok) > width:
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(tok[:width])
            tok = tok[width:]
        if len(cur) + len(tok) <= width:
            cur += tok
        else:
            lines.append(cur)
            cur = tok
    if cur:
        lines.append(cur)
    return "<br/>".join(esc(x) for x in lines[:max_lines])


def _table(data, widths, header=True, zebra=True, extra=None):
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.3, LINE),
             ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    if header:
        style.append(("BACKGROUND", (0, 0), (-1, 0), NAVY))
    if zebra:
        style += [("ROWBACKGROUNDS", (0, 1 if header else 0), (-1, -1), [colors.white, ZEBRA])]
    t.setStyle(TableStyle(style + (extra or [])))
    return t


def section(title: str, S, need_mm: float = 115):
    """Heading that moves to a fresh page when there isn't room for the chart that follows (no orphaned headings)."""
    return [CondPageBreak(need_mm * mm), Paragraph(title, S["H1"])]


def _chart(co: ChartOut | None, S, max_h=105 * mm):
    if co is None:
        return []
    w = CONTENT_W
    h = w * co.aspect
    if h > max_h:
        w, h = w * max_h / h, max_h
    return [KeepTogether([Image(BytesIO(co.png), width=w, height=h), Paragraph(esc(co.takeaway), S["caption"])])]


def _kpis(result, S):
    src = result["sites"][result["source_key"]]
    comps = [c for c in result["competitors"] if c["matched"]]
    matched = sum(c["matched"] for c in comps)
    gap = sum(c["avg_gap_pct"] for c in comps) / len(comps) if comps else None
    position = "n/a" if gap is None else (f"{abs(gap):.1f}% {'lower' if gap > 0 else 'higher'}")
    warnings = sum(1 for a in result["alerts"] if a["level"] == "warning")
    cat_comps = result.get("category_comparisons", {})
    total_cat = sum(len(v) for v in cat_comps.values())
    cells = [(str(src["product_count"]), "of your products read"), (str(matched), "matched product comparisons"),
             (position, "your prices vs competitors (average)"), (str(warnings), "price-undercut warnings"),
             (str(total_cat), "category comparisons")]
    n = len(cells)
    t = Table([[Paragraph(v, S["kpi"]) for v, _ in cells], [Paragraph(esc(lab), S["kpil"]) for _, lab in cells]], colWidths=[CONTENT_W / n] * n)
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.5, LINE), ("BACKGROUND", (0, 0), (-1, -1), ZEBRA),
                           ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (0, 0), 8), ("BOTTOMPADDING", (0, 1), (-1, 1), 8)]))
    return t


def _price_table(result, S, limit=120):
    comps = [c for c in result["competitors"] if c["matched"]]
    head = [Paragraph("Product (yours)", S["cellb"]), Paragraph("Your price", S["cellb"])] + [Paragraph(wrap_name(c["name"]), S["cellb"]) for c in comps]
    data = [head]
    for r in result["rows"][:limit]:
        row = [Paragraph(esc(r["source"]["title"][:70]), S["cell"]), Paragraph(inr(r["source"]["price"]), S["cell"])]
        for c in comps:
            v = r["competitors"].get(c["key"])
            if not v:
                row.append(Paragraph("–", S["cell"]))
                continue
            col = "#D55E00" if v["gap_pct"] < -1 else "#0072B2" if v["gap_pct"] > 1 else "#5d6678"
            flags = (" · out" if v["in_stock"] is False else "") + (" · low" if v["low_stock"] else "")
            row.append(Paragraph(f'{inr(v["price"])}<br/><font color="{col}" size="6.6">{v["gap_pct"]:+.1f}%{flags}</font>', S["cell"]))
        data.append(row)
    first = CONTENT_W * 0.30
    rest = (CONTENT_W - first - 17 * mm) / max(len(comps), 1)
    return _table(data, [first, 17 * mm] + [rest] * len(comps))


def _balanced_alerts(alerts: list[dict], limit: int) -> list[dict]:
    """Most urgent first, but capped per type so one kind of alert cannot crowd out the rest."""
    caps = {"warning": 10, "risk": 6, "opportunity": 6, "info": 3}
    used: dict[str, int] = {}
    out = []
    for a in alerts:
        if used.get(a["level"], 0) < caps[a["level"]] and len(out) < limit:
            used[a["level"]] = used.get(a["level"], 0) + 1
            out.append(a)
    return out


def _alerts_table(result, S, limit=25):
    labels = {"warning": "Price threat", "risk": "Risk", "opportunity": "Opportunity", "info": "Info"}
    colr = {"warning": "#D55E00", "risk": "#b42318", "opportunity": "#0072B2", "info": "#5d6678"}
    data = [[Paragraph(h, S["cellb"]) for h in ("Type", "Site", "Alert")]]
    for a in _balanced_alerts(result["alerts"], limit):
        data.append([Paragraph(f'<font color="{colr[a["level"]]}"><b>{labels[a["level"]]}</b></font>', S["cell"]), Paragraph(esc(a["site"]), S["cell"]), Paragraph(esc(a["message"]), S["cell"])])
    return _table(data, [24 * mm, 34 * mm, CONTENT_W - 58 * mm])


def _site_order(result: dict) -> list[str]:
    return [result["source_key"], *[c["key"] for c in result["competitors"]]]


def _name(result: dict, key: str) -> str:
    return (result["sites"].get(key) or {}).get("name") or next((d["name"] for d in result["data_quality"] if d["key"] == key), key)


def _ai_block(result: dict, S) -> list:
    ai = result.get("ai") or {}
    if ai.get("status") == "failed":
        return [Paragraph(f"AI summary was not available ({esc(ai.get('error', 'unknown error'))}). The rule-based insights below are shown instead.", S["caption"])]
    if ai.get("status") != "ok":
        return []
    out = [Paragraph("AI-written summary", S["H2"]),
           Paragraph(f"Written by {esc(ai.get('model'))} from the numbers in this report. Statements containing numbers that were not in the data were removed "
                     f"({ai.get('removed_statements', 0)}). Review before acting.", S["caption"])]
    if ai.get("executive_summary"):
        out.append(Paragraph(esc(ai["executive_summary"]), S["base"]))
        out.append(Spacer(1, 3 * mm))
    for f in ai.get("key_findings", []):
        out.append(Paragraph(f"<b>{esc(f['title'])}.</b> {esc(f['text'])}", S["bullet"], bulletText="•"))
    if ai.get("recommended_actions"):
        pc = {"high": "#b42318", "medium": "#9a6700", "low": "#5d6678"}
        data = [[Paragraph(h, S["cellb"]) for h in ("Priority", "Recommended action")]]
        for a in ai["recommended_actions"]:
            data.append([Paragraph(f'<font color="{pc[a["priority"]]}"><b>{a["priority"].title()}</b></font>', S["cell"]),
                         Paragraph(f"<b>{esc(a['action'])}</b><br/><font color='#5d6678'>{esc(a.get('rationale', ''))}</font>", S["cell"])])
        out += [Spacer(1, 2 * mm), _table(data, [20 * mm, CONTENT_W - 20 * mm])]
    for note_title, items in (("Risks", ai.get("risks") or []),):
        if items:
            out.append(Paragraph(note_title, S["H2"]))
            out += [Paragraph(esc(i), S["bullet"], bulletText="•") for i in items]
    if ai.get("per_competitor_notes"):
        out.append(Paragraph("Notes per competitor", S["H2"]))
        out += [Paragraph(f"<b>{esc(k)}:</b> {esc(v)}", S["bullet"], bulletText="•") for k, v in ai["per_competitor_notes"].items()]
    return out


def _offers_table(result: dict, S):
    sig = result.get("signals") or {}
    data = [[Paragraph(h, S["cellb"]) for h in ("Site", "Free shipping", "Returns", "Warranty", "Payments", "Offers and codes found")]]
    for key in _site_order(result):
        g = sig.get(key)
        if not g:
            continue
        ship = "All orders" if g.get("free_shipping_all") else f"Above ₹{g['free_shipping_threshold']:,}" if g.get("free_shipping_threshold") else "Not found"
        pol = g.get("policies") or {}
        offers = "<br/>".join(esc((f"[{o['code']}] " if o.get("code") else "") + o["text"][:70]) for o in g.get("offers", [])[:3]) or "None found"
        data.append([Paragraph(esc(_name(result, key)) + (" <b>(you)</b>" if key == result["source_key"] else ""), S["cell"]), Paragraph(ship, S["cell"]),
                     Paragraph(f"{pol['return_window_days']} days" if pol.get("return_window_days") else "Not found", S["cell"]), Paragraph(esc(pol.get("warranty") or "Not found"), S["cell"]),
                     Paragraph(esc(", ".join(g.get("payment_methods", [])[:4]) or "Not found"), S["cell"]), Paragraph(offers, S["cell"])])
    if len(data) == 1:
        return None
    return _table(data, [26 * mm, 22 * mm, 16 * mm, 17 * mm, 30 * mm, CONTENT_W - 111 * mm])


def _marketing_tables(result: dict, S) -> list:
    sig, social, links = result.get("signals") or {}, result.get("social") or {}, result.get("ad_links") or {}
    tags = [[Paragraph(h, S["cellb"]) for h in ("Site", "Advertising tags", "Analytics", "Email, reviews, checkout, support")]]
    soc = [[Paragraph(h, S["cellb"]) for h in ("Site", "Social profiles found", "YouTube", "Public ad libraries")]]
    for key in _site_order(result):
        n = esc(_name(result, key)) + (" <b>(you)</b>" if key == result["source_key"] else "")
        g = sig.get(key) or {}
        stack = g.get("marketing_stack") or {}
        other = ", ".join(sum((stack.get(c, []) for c in ("engagement", "reviews", "checkout", "support")), [])) or "None found"
        tags.append([Paragraph(n, S["cell"]), Paragraph(esc(", ".join(stack.get("advertising", [])) or "None found"), S["cell"]),
                     Paragraph(esc(", ".join(stack.get("analytics", [])) or "None found"), S["cell"]), Paragraph(esc(other), S["cell"])])
        sl = (social.get(key) or {}).get("links") or g.get("social_links") or {}
        yt = (social.get(key) or {}).get("youtube")
        if yt and yt.get("status") == "ok":
            ytxt = f"{yt['followers']:,} subscribers" if yt.get("followers") else "Subscribers hidden"
            ytxt += f"<br/>{yt['posts_last_30d']} videos in 30 days" if yt.get("posts_last_30d") is not None else ""
            ytxt += f"<br/>{yt['engagement_rate_pct']}% engagement" if yt.get("engagement_rate_pct") is not None else ""
        elif yt:
            ytxt = esc(yt.get("error") or "Not collected")
        else:
            ytxt = "Linked, not collected (set YOUTUBE_API_KEY)" if "youtube" in sl else "No channel found on homepage"
        al = links.get(key) or {}
        libs = " · ".join(f'<a href="{esc(al[k])}" color="#2563eb">{lab}</a>' for k, lab in (("meta", "Meta Ad Library"), ("google", "Google Ads Transparency")) if al.get(k))
        soc.append([Paragraph(n, S["cell"]), Paragraph(esc(", ".join(f"{p} ({v['handle']})" for p, v in sl.items()) or "None found"), S["cell"]), Paragraph(ytxt, S["cell"]), Paragraph(libs or "–", S["cell"])])
    return [Paragraph("Tracking and marketing tools found on each homepage", S["H2"]), _table(tags, [28 * mm, 40 * mm, 40 * mm, CONTENT_W - 108 * mm]),
            Paragraph("Ad pixels and tags suggest paid campaigns or retargeting but do not prove that ads are live. Spend, impressions and ad creatives are not collected.", S["caption"]),
            Paragraph("Social profiles and public ad libraries", S["H2"]), _table(soc, [28 * mm, 50 * mm, 40 * mm, CONTENT_W - 118 * mm]),
            Paragraph("Meta's official API does not return commercial ads for India, so these links open the free public libraries (set to India) for you to review.", S["caption"])]


def _changes_block(result: dict, charts: dict, S) -> list:
    ch = result.get("changes") or {}
    if not ch.get("has_history"):
        return [Paragraph("No earlier analysis of these sites was found. Run the analysis again later, or create a monitor on the website, and this section will show what changed.", S["base"])]
    out = [Paragraph(f"Compared with the previous analysis ({_fmt_date(ch.get('previous_run_at'))}).", S["caption"])]
    data = [[Paragraph(h, S["cellb"]) for h in ("Site", "New products", "Removed", "Price drops", "Price rises", "Went out of stock", "New offers")]]
    for key in _site_order(result):
        c = (ch.get("sites") or {}).get(key)
        if c:
            data.append([Paragraph(esc(_name(result, key)), S["cell"]), Paragraph(str(c["new_count"]), S["cell"]), Paragraph(str(c["removed_count"]), S["cell"]), Paragraph(str(c["price_drop_count"]), S["cell"]),
                         Paragraph(str(c["price_increase_count"]), S["cell"]), Paragraph(str(len(c["went_out_of_stock"])), S["cell"]), Paragraph(str(len(c["new_offers"])), S["cell"])])
    out.append(_table(data, [CONTENT_W * 0.28] + [CONTENT_W * 0.12] * 6))
    drops = sorted(((c["price_drops"][i], key) for key, c in (ch.get("sites") or {}).items() for i in range(len(c["price_drops"]))), key=lambda t: t[0]["change_pct"])[:10]
    if drops:
        d2 = [[Paragraph(h, S["cellb"]) for h in ("Site", "Product", "Was", "Now", "Change")]]
        for d, key in drops:
            d2.append([Paragraph(esc(_name(result, key)), S["cell"]), Paragraph(esc(d["title"][:60]), S["cell"]), Paragraph(inr(d["old"]), S["cell"]), Paragraph(inr(d["new"]), S["cell"]),
                       Paragraph(f'<font color="#D55E00">{d["change_pct"]:+.1f}%</font>', S["cell"])])
        out += [Paragraph("Biggest price drops", S["H2"]), _table(d2, [30 * mm, CONTENT_W - 30 * mm - 60 * mm, 20 * mm, 20 * mm, 20 * mm])]
    if any(c.get("cap_reached") for c in (ch.get("sites") or {}).values()):
        out.append(Paragraph("Product limit reached for at least one site, so new and removed counts may be incomplete.", S["caption"]))
    out += _chart(charts.get("price_history"), S, max_h=140 * mm) + _chart(charts.get("change_frequency"), S)
    return out


def build_pdf(result: dict, meta: dict | None = None) -> bytes:
    """result: output of analyze() plus the optional ai/changes/history parts; meta: {finished_at, ...}."""
    if not result.get("comparison_available"):
        raise ValueError("No comparison available: the report needs your site and at least one competitor to have been read.")
    register_fonts()
    meta = meta or {}
    S = _styles()
    charts = build_charts(result)
    src = result["sites"][result["source_key"]]
    n_comp = len(result["competitors"])
    title = f"Competitor Price Intelligence Report: {src['name']}"
    buf = BytesIO()
    doc = ReportDoc(buf, title=title, footer_text=f"Competitor Price Intelligence · {src['name']} · {_fmt_date(result.get('generated_at'))}")
    counter = iter(range(1, 30))
    story: list = []

    # ---- cover
    story += [Spacer(1, 38 * mm), Paragraph("Competitor Price<br/>Intelligence Report", S["title"]), Spacer(1, 6 * mm),
              Paragraph(f"{esc(src['name'])} compared with {n_comp} competitor{'s' if n_comp != 1 else ''}", S["subtitle"]), Spacer(1, 62 * mm)]
    rows = [[Paragraph(h, S["cellb"]) for h in ("Website", "Role", "Platform", "Products read", "Status")]]
    for d in result["data_quality"]:
        rows.append([Paragraph(esc(d["name"]), S["cell"]), Paragraph(d["role"], S["cell"]), Paragraph(esc(d["platform"] or "–"), S["cell"]), Paragraph(str(d["products"]), S["cell"]),
                     Paragraph("OK" if d["status"] == "ok" else '<font color="#b42318">Failed</font>', S["cell"])])
    story += [Paragraph("Websites analysed", S["H2"]), _table(rows, [CONTENT_W * 0.36, CONTENT_W * 0.14, CONTENT_W * 0.18, CONTENT_W * 0.17, CONTENT_W * 0.15]), Spacer(1, 6 * mm),
              Paragraph(f"Data collected: {_fmt_date(meta.get('finished_at') or result.get('generated_at'))}", S["small"]),
              Paragraph("Built from publicly visible website data only. Missing information is marked as unknown, never estimated.", S["small"]),
              NextPageTemplate("body"), PageBreak()]

    # ---- contents
    toc = TableOfContents()
    toc.levelStyles = [ParagraphStyle("toc0", fontName=FONT, fontSize=10, leading=18, leftIndent=0)]
    toc.dotsMinLevel = 0
    story += [Paragraph("Contents", ParagraphStyle("ct", parent=S["base"], fontName=FONT_B, fontSize=17, leading=22, textColor=NAVY, spaceAfter=10)), toc, PageBreak()]

    # ---- executive summary
    story += [Paragraph(f"{next(counter)}. Executive summary", S["H1"]), _kpis(result, S), Spacer(1, 4 * mm)]
    story += _ai_block(result, S)
    story.append(Paragraph("Key insights", S["H2"]))
    for i in result["insights"]:
        story.append(Paragraph(f"<b>{esc(i['title'])}.</b> {esc(i['text'])}", S["bullet"], bulletText="•"))
    if result["recommendations"]:
        story.append(Paragraph("Suggested actions", S["H2"]))
        pc = {"high": "#b42318", "medium": "#9a6700", "low": "#5d6678"}
        data = [[Paragraph(h, S["cellb"]) for h in ("Priority", "Action")]]
        for r in result["recommendations"]:
            data.append([Paragraph(f'<font color="{pc[r["priority"]]}"><b>{r["priority"].title()}</b></font>', S["cell"]),
                         Paragraph(f"<b>{esc(r['action'])}</b><br/><font color='#5d6678'>{esc(r['detail'])}</font>", S["cell"])])
        story.append(_table(data, [20 * mm, CONTENT_W - 20 * mm]))
    story.append(PageBreak())

    # ---- price comparison
    story.append(Paragraph(f"{next(counter)}. Price comparison", S["H1"]))
    for key in ("price_bars", "gap_heatmap", "position", "distribution"):
        story += _chart(charts.get(key), S)
    story.append(PageBreak())

    # ---- discounts, stock, catalogue, positioning
    story += section(f"{next(counter)}. Discounts", S, 95) + _chart(charts.get("discounts"), S)
    story += section(f"{next(counter)}. Stock and availability", S, 95) + _chart(charts.get("stock"), S)
    story.append(PageBreak())
    story += section(f"{next(counter)}. Catalogue and customer ratings", S, 95) + _chart(charts.get("coverage"), S) + _chart(charts.get("ratings"), S)
    story += section(f"{next(counter)}. Competitive positioning", S, 130) + _chart(charts.get("radar"), S, max_h=120 * mm)

    # ---- offers, marketing, changes
    offers = _offers_table(result, S)
    if offers is not None:
        story += section(f"{next(counter)}. Offers, shipping and policies", S, 80)
        story += [Paragraph("Read from each homepage and its return, shipping and warranty pages. “Not found” means we did not see it, not that it does not exist.", S["caption"]), offers]
    if result.get("signals") or result.get("social"):
        story += section(f"{next(counter)}. Marketing and social", S, 90) + _marketing_tables(result, S) + _chart(charts.get("social"), S)
    story += section(f"{next(counter)}. Changes and trends", S, 90) + _changes_block(result, charts, S)

    # ---- alerts
    story.append(PageBreak())
    story.append(Paragraph(f"{next(counter)}. Alerts", S["H1"]))
    if result["alerts"]:
        shown = len(_balanced_alerts(result["alerts"], 25))
        story += [Paragraph(f"Showing {shown} of {len(result['alerts'])} alerts: the most urgent of each type, price threats first.", S["caption"]), _alerts_table(result, S)]
    else:
        story.append(Paragraph("No alerts.", S["base"]))
    story.append(PageBreak())

    # ---- appendices
    letters = iter("ABCDEF")
    story += [Paragraph(f"Appendix {next(letters)}: Matched product prices", S["H1"]),
              Paragraph("Percent shows the competitor's price relative to yours. Orange = cheaper than you, blue = costlier. “out” = out of stock, “low” = low-stock flag.", S["caption"]),
              _price_table(result, S)]
    if len(result["rows"]) > 120:
        story.append(Paragraph(f"Showing 120 of {len(result['rows'])} matched products.", S["caption"]))
    # Category comparison appendix - critical for brand-vs-brand with 0 product matches
    cat_data = result.get("category_comparisons", {})
    all_cat_rows = [(k, v) for k, v in cat_data.items() if v]
    if all_cat_rows:
        story += [Spacer(1, 6 * mm), CondPageBreak(60 * mm),
                  Paragraph(f"Appendix {next(letters)}: Category comparison", S["H1"]),
                  Paragraph(
                      "Compares average prices per shared product category. "
                      "Useful for brand-vs-brand analysis where individual product names differ. "
                      "Orange = competitor cheaper, blue = competitor more expensive.",
                      S["caption"])]
        for comp_key, cat_rows in all_cat_rows:
            comp_name = next((c["name"] for c in result["competitors"] if c["key"] == comp_key), comp_key)
            if len(all_cat_rows) > 1:
                story.append(Paragraph(f"vs {esc(comp_name)}", S["H2"]))
            hdr = [Paragraph(h, S["cellb"]) for h in (
                "Category", "Your prods", "Their prods",
                "Your avg", "Their avg", "Gap", "On sale Y/T")]
            tdata = [hdr]
            for m in cat_rows:
                gp = m.get("price_gap_pct")
                gap_str = "–" if gp is None else f"{'+' if gp > 0 else ''}{gp:.1f}%"
                gap_note = "" if gp is None else (" pricier" if gp > 0 else " cheaper")
                sale_s = m.get("source_on_sale_pct")
                sale_c = m.get("comp_on_sale_pct")
                sale_str = "–" if sale_s is None else (f"{sale_s:.0f}%/{sale_c:.0f}%" if sale_c is not None else f"{sale_s:.0f}%")
                gap_color = "#D55E00" if gp is not None and gp < -1 else "#0072B2" if gp is not None and gp > 1 else "#5d6678"
                tdata.append([
                    Paragraph(esc(m["category"]), S["cell"]),
                    Paragraph(str(m["source_count"]), S["cell"]),
                    Paragraph(str(m["comp_count"]), S["cell"]),
                    Paragraph(inr(m["source_avg_price"]), S["cell"]),
                    Paragraph(inr(m["comp_avg_price"]), S["cell"]),
                    Paragraph(f'<font color="{gap_color}"><b>{esc(gap_str)}</b></font>{esc(gap_note)}', S["cell"]),
                    Paragraph(sale_str, S["cell"]),
                ])
            cw = CONTENT_W / 7
            story.append(_table(tdata, [cw * 1.6, cw * 0.75, cw * 0.75, cw, cw, cw * 0.95, cw * 0.95]))

    if result["review_matches"]:
        story += [Spacer(1, 6 * mm), CondPageBreak(60 * mm), Paragraph(f"Appendix {next(letters)}: Possible matches to double-check", S["H1"]),
                  Paragraph("Similar-looking pairs that were not confident enough to include in the comparison.", S["caption"])]
        data = [[Paragraph(h, S["cellb"]) for h in ("Competitor", "Your product", "Their product", "Match")]]
        for m in result["review_matches"][:25]:
            data.append([Paragraph(esc(m["competitor"]), S["cell"]), Paragraph(f"{esc(m['source_title'][:60])}<br/>{inr(m['source_price'])}", S["cell"]),
                         Paragraph(f"{esc(m['competitor_title'][:60])}<br/>{inr(m['competitor_price'])}", S["cell"]), Paragraph(f"{m['confidence']:.0%}", S["cell"])])
        story.append(_table(data, [28 * mm, (CONTENT_W - 44 * mm) / 2, (CONTENT_W - 44 * mm) / 2, 16 * mm]))
    story += [Spacer(1, 6 * mm), CondPageBreak(70 * mm), Paragraph(f"Appendix {next(letters)}: Data quality, method and limitations", S["H1"])]
    data = [[Paragraph(h, S["cellb"]) for h in ("Site", "Status", "Method", "Products", "Details")]]
    for d in result["data_quality"]:
        data.append([Paragraph(esc(d["name"]), S["cell"]), Paragraph(d["status"], S["cell"]), Paragraph(esc(d["method"] or "–"), S["cell"]), Paragraph(str(d["products"]), S["cell"]), Paragraph(esc(d["error"] or ""), S["cell"])])
    story.append(_table(data, [34 * mm, 16 * mm, 38 * mm, 18 * mm, CONTENT_W - 106 * mm]))
    notes = [
        "Products are matched by barcode (GTIN), then SKU, then brand plus model number, then title similarity. Pairs with conflicting brands, model numbers or sizes are rejected. "
        f"Only matches at or above {result['match_threshold']:.0%} confidence are used in comparisons.",
        "“Price index” is the average competitor price over matched products with your price set to 100. Below 100 means the competitor is cheaper.",
        "Stock is shown only when a site publishes it. Otherwise it is reported as unknown. Exact stock counts are rarely public.",
        "Prices are as shown to an anonymous visitor at collection time and can differ by region, login or cookies.",
        "Advertising: only the presence of ad tags and links to the public ad libraries are provided. Ad spend, impressions and creatives are not collected.",
        "Social: only YouTube channel statistics are collected, and only when a channel is linked from the homepage and a YouTube API key is configured.",
        "Offers and policies are read from homepage and policy text with pattern matching and can miss or misread unusual wording.",
    ]
    story += [CondPageBreak(75 * mm), Paragraph("How to read this report", S["H2"])]
    story += [Paragraph(esc(n), S["bullet"], bulletText="•") for n in notes]

    doc.multiBuild(story)
    return buf.getvalue()
