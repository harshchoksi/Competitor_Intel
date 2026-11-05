"""Optional AI second opinion on BORDERLINE product matches (the 'review' band between the review floor and the threshold).

Deterministic matching already handles clear cases. Gemini is only asked about the uncertain ones, in small batches, and
only judges "same product, ignoring pack size?" yes/no. A 'yes' promotes the pair to matched (method 'ai'); a 'no' removes
it. Pairs the model does not answer stay as they were. Cost is capped by `max_pairs` per competitor."""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from loguru import logger

from src.analysis.matching import Match
from src.analysis.types import SiteData
from src.llm.gemini import GeminiClient, LLMError

SYSTEM = """You compare product listings from two online shops and decide if each pair is the SAME retail product.
Same product = same brand (if any), same type, same variety/flavour/cut/fat level/gender/model. A different pack size of the
same product still counts as the same product. Different flavours, cuts (breast vs thigh), meats, diet vs regular, or models are NOT the same.
Answer ONLY with JSON: {"verdicts":[{"id":<number>,"same":true|false}]} and include every id you were given."""


def make_verifier(client: GeminiClient, *, max_pairs: int = 80, batch: int = 20, max_seconds: float = 30.0, workers: int = 3):
    """-> function(matches, source, competitor) -> matches, suitable for analyze(verifier=...).

    The AI check is a bonus, never a reason for an analysis to hang or crawl: batches run `workers` at a time, it stops for good after the
    first failure (rate limit, network, bad key) and after `max_seconds` in total across all competitors."""
    state = {"started": None, "disabled": False}

    def over_budget() -> bool:
        if state["disabled"]:
            return True
        if time.monotonic() - state["started"] > max_seconds:
            logger.warning("AI match check stopped: time budget of {:.0f}s used", max_seconds)
            state["disabled"] = True
        return state["disabled"]

    def ask(chunk: list[Match], source: SiteData, comp: SiteData) -> dict[tuple[int, int], bool]:
        if over_budget():
            return {}
        payload = [{"id": n, "a": _desc(source, m.source_idx), "b": _desc(comp, m.comp_idx)} for n, m in enumerate(chunk)]
        try:
            out = client.generate_json(SYSTEM, "PAIRS:\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        except LLMError as exc:
            logger.warning("AI match check stopped: {}", exc)
            state["disabled"] = True
            return {}
        found: dict[tuple[int, int], bool] = {}
        for v in out.get("verdicts") or []:
            try:
                m = chunk[int(v["id"])]
                found[(m.source_idx, m.comp_idx)] = bool(v["same"])
            except (KeyError, ValueError, IndexError, TypeError):
                continue
        return found

    def verify(matches: list[Match], source: SiteData, comp: SiteData) -> list[Match]:
        if state["started"] is None:
            state["started"] = time.monotonic()
        if state["disabled"]:
            return matches
        pending = sorted((m for m in matches if m.status == "review"), key=lambda m: -m.confidence)[:max_pairs]
        chunks = [pending[i:i + batch] for i in range(0, len(pending), batch)]
        verdicts: dict[tuple[int, int], bool] = {}
        if len(chunks) <= 1:
            for c in chunks:
                verdicts.update(ask(c, source, comp))
        else:
            with ThreadPoolExecutor(max_workers=min(workers, len(chunks))) as pool:
                for found in pool.map(lambda c: ask(c, source, comp), chunks):
                    verdicts.update(found)
        result: list[Match] = []
        for m in matches:
            verdict = verdicts.get((m.source_idx, m.comp_idx))
            if verdict is True:
                result.append(replace(m, status="matched", method="ai", confidence=max(m.confidence, 0.9), reasons=(*m.reasons, "confirmed by AI check")))
            elif verdict is False:
                continue  # AI says different: drop it
            else:
                result.append(m)
        return result

    return verify


def _desc(site: SiteData, idx: int) -> dict:
    p = site.products[idx]
    d = {"title": p.title[:140]}
    if p.brand:
        d["brand"] = p.brand[:60]
    return d
