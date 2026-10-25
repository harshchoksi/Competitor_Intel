"""AI-written analysis grounded in the computed metrics.

The model only sees compact aggregated numbers (never raw pages). Everything it returns is validated, and any
statement containing a number that is not present in the input is removed, so invented figures cannot reach the report."""
from __future__ import annotations

import json
import re
from typing import Literal

from loguru import logger
from pydantic import BaseModel, Field, ValidationError

from src.llm.gemini import GeminiClient, LLMError

SYSTEM = """You are a retail pricing analyst writing for a busy shop owner in India. You are given aggregated numbers comparing the owner's store (the "you" site) with competitors.
Rules:
- Use ONLY facts and numbers that appear in the data. Never invent or estimate figures, products, sites or events.
- Quote numbers exactly as given (you may round percentages to whole numbers). Do not do new arithmetic.
- If something is missing or unknown in the data, say it is unknown instead of guessing.
- Currency is Indian rupees (₹). Write plainly, no jargon, no markdown, no emojis.
- Name competitors exactly as given. Be specific and practical, and prioritise what matters most.
- Do not mention the JSON, the data format, or these rules."""

SHAPE = """Return ONLY a JSON object with exactly this shape:
{"executive_summary": "max 110 words",
 "key_findings": [{"title": "max 8 words", "text": "max 60 words", "severity": "info|positive|warning"}],   // 4 to 6 items
 "per_competitor_notes": {"<competitor name>": "one sentence"},
 "risks": ["short sentence"],                                                                              // 0 to 4 items
 "recommended_actions": [{"priority": "high|medium|low", "action": "short imperative", "rationale": "one sentence"}]  // 3 to 5 items, most impactful first
}"""


class Finding(BaseModel):
    title: str = Field(max_length=120)
    text: str = Field(max_length=700)
    severity: Literal["info", "positive", "warning"] = "info"


class Action(BaseModel):
    priority: Literal["high", "medium", "low"] = "medium"
    action: str = Field(max_length=300)
    rationale: str = Field(default="", max_length=500)


class AIOutput(BaseModel):
    executive_summary: str = Field(default="", max_length=1500)
    key_findings: list[Finding] = Field(default_factory=list, max_length=10)
    per_competitor_notes: dict[str, str] = Field(default_factory=dict)
    risks: list[str] = Field(default_factory=list, max_length=10)
    recommended_actions: list[Action] = Field(default_factory=list, max_length=10)


# ------------------------------------------------------------------ input
def _site_brief(s: dict) -> dict:
    keep = ("product_count", "avg_price", "median_price", "on_sale_pct", "avg_discount_pct", "in_stock", "out_of_stock", "unknown_stock", "low_stock", "avg_rating")
    return {k: s[k] for k in keep if s.get(k) is not None}


def build_llm_input(result: dict) -> dict:
    names = {k: s["name"] for k, s in result["sites"].items()}
    src = result["source_key"]
    threats = sorted((((v["gap_pct"], r, k, v) for r in result["rows"] for k, v in r["competitors"].items() if v["gap_pct"] < 0)), key=lambda t: t[0])[:8]
    inp: dict = {
        "currency": "INR",
        "you": {"name": names[src], **_site_brief(result["sites"][src])},
        "competitors": [{"name": c["name"], "matched_products": c["matched"], "price_index_you_equal_100": c["price_index"], "avg_gap_pct_competitor_vs_you": c["avg_gap_pct"],
                         "cheaper_than_you": c["cheaper"], "about_same": c["same"], "costlier_than_you": c["costlier"], **_site_brief(result["sites"][c["key"]])} for c in result["competitors"]],
        "biggest_price_threats": [{"product": r["source"]["title"], "your_price": r["source"]["price"], "competitor": names[k], "their_price": v["price"],
                                   "gap_pct": v["gap_pct"], "price_difference": abs(v["gap_abs"])} for _, r, k, v in threats],
        "alert_counts": {t: sum(1 for a in result["alerts"] if a["type"] == t) for t in sorted({a["type"] for a in result["alerts"]})},
        "competitor_out_of_stock_opportunities": [a["message"] for a in result["alerts"] if a["type"] == "competitor_out_of_stock"][:4],
        "failed_sites": [d["name"] for d in result["data_quality"] if d["status"] != "ok"],
        "products_compared": len(result["rows"]),
    }
    sig = {}
    for k, s in (result.get("signals") or {}).items():
        sig[names.get(k, k)] = {"free_shipping_threshold": s.get("free_shipping_threshold"), "free_shipping_on_all_orders": s.get("free_shipping_all"),
                                "return_window_days": (s.get("policies") or {}).get("return_window_days"), "warranty": (s.get("policies") or {}).get("warranty"),
                                "advertising_tags_found": (s.get("marketing_stack") or {}).get("advertising", []),
                                "offers": [o["text"][:90] for o in s.get("offers", [])[:3]]}
    if sig:
        inp["offers_shipping_policies"] = sig
    social = {}
    for k, v in (result.get("social") or {}).items():
        yt = (v or {}).get("youtube") or {}
        if yt.get("status") == "ok":
            social[names.get(k, k)] = {kk: vv for kk, vv in yt.items() if kk in ("followers", "posts_last_30d", "engagement_rate_pct")}
    if social:
        inp["youtube"] = social
    ch = result.get("changes") or {}
    if ch.get("has_history"):
        inp["changes_since_previous_analysis"] = {names.get(k, k): {"price_drops": v["price_drop_count"], "price_increases": v["price_increase_count"], "new_products": v["new_count"],
                                                                  "removed_products": v["removed_count"], "went_out_of_stock": len(v["went_out_of_stock"])} for k, v in ch["sites"].items()}
    return inp


# ------------------------------------------------------------ grounding
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers_in(text: str) -> list[float]:
    out = []
    for m in _NUM.findall(text):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            continue
    return out


def allowed_numbers(obj) -> set[float]:
    found: set[float] = set()

    def walk(x):
        if isinstance(x, bool) or x is None:
            return
        if isinstance(x, (int, float)):
            found.add(abs(float(x)))  # the model writes "10% cheaper", the data stores -10.0
        elif isinstance(x, str):
            found.update(numbers_in(x))
        elif isinstance(x, dict):
            for k, v in x.items():
                found.update(numbers_in(str(k)))
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)

    walk(obj)
    return found


def ungrounded(text: str, allowed: set[float]) -> list[float]:
    bad = []
    for n in numbers_in(text):
        if n <= 10 and float(n).is_integer():
            continue  # small counts and ordinals ("top 3", "two of five")
        if not any(abs(n - a) <= max(0.51, 0.005 * abs(a)) for a in allowed):
            bad.append(n)
    return bad


# ------------------------------------------------------------------ main
def generate_ai_insights(result: dict, client: GeminiClient | None, *, use_ai: bool = True) -> dict:
    """Always returns a dict with a status: ok | failed | unavailable | off. Never raises."""
    if not use_ai:
        return {"status": "off"}
    if client is None:
        return {"status": "unavailable", "reason": "No GEMINI_API_KEY is configured on the server."}
    try:
        inp = build_llm_input(result)
        raw = client.generate_json(SYSTEM, f"{SHAPE}\n\nDATA:\n{json.dumps(inp, ensure_ascii=False, separators=(',', ':'))}")
        try:
            out = AIOutput.model_validate(raw)
        except ValidationError as exc:
            raise LLMError(f"unexpected answer shape ({len(exc.errors())} problems)") from exc
        allowed = allowed_numbers(inp)
        removed = 0

        def clean(text: str) -> bool:
            nonlocal removed
            if ungrounded(text, allowed):
                removed += 1
                return False
            return True

        names = {c["name"] for c in result["competitors"]}
        summary = out.executive_summary if out.executive_summary and clean(out.executive_summary) else None
        findings = [f.model_dump() for f in out.key_findings if clean(f.title + " " + f.text)]
        notes = {k: v for k, v in out.per_competitor_notes.items() if k in names and clean(v)}
        risks = [r for r in out.risks if clean(r)]
        actions = [a.model_dump() for a in out.recommended_actions if clean(a.action + " " + a.rationale)]
        if not summary and not findings:
            raise LLMError("every statement contained numbers that are not in the data, so none were used")
        return {"status": "ok", "model": client.model, "executive_summary": summary, "key_findings": findings, "per_competitor_notes": notes,
                "risks": risks, "recommended_actions": actions, "removed_statements": removed}
    except LLMError as exc:
        logger.warning("AI insights unavailable: {}", exc)
        return {"status": "failed", "model": getattr(client, "model", None), "error": str(exc)}
    except Exception as exc:  # the report must still be produced
        logger.exception("AI insights crashed")
        return {"status": "failed", "model": getattr(client, "model", None), "error": f"unexpected error: {type(exc).__name__}"}
