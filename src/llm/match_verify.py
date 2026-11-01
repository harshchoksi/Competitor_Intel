"""Optional AI second opinion on BORDERLINE product matches (the 'review' band between the review floor and the threshold).

Deterministic matching already handles clear cases. Gemini is only asked about the uncertain ones, in small batches, and
only judges "same product, ignoring pack size?" yes/no. A 'yes' promotes the pair to matched (method 'ai'); a 'no' removes
it. Pairs the model does not answer stay as they were. Cost is capped by `max_pairs` per competitor."""
from __future__ import annotations

import json
from dataclasses import replace

from loguru import logger

from src.analysis.matching import Match
from src.analysis.types import SiteData
from src.llm.gemini import GeminiClient, LLMError

SYSTEM = """You compare product listings from two online shops and decide if each pair is the SAME retail product.
Same product = same brand (if any), same type, same variety/flavour/cut/fat level/gender/model. A different pack size of the
same product still counts as the same product. Different flavours, cuts (breast vs thigh), meats, diet vs regular, or models are NOT the same.
Answer ONLY with JSON: {"verdicts":[{"id":<number>,"same":true|false}]} and include every id you were given."""


def make_verifier(client: GeminiClient, *, max_pairs: int = 120, batch: int = 20):
    """-> function(matches, source, competitor) -> matches, suitable for analyze(verifier=...)."""

    def verify(matches: list[Match], source: SiteData, comp: SiteData) -> list[Match]:
        pending = sorted((m for m in matches if m.status == "review"), key=lambda m: -m.confidence)[:max_pairs]
        verdicts: dict[tuple[int, int], bool] = {}
        for start in range(0, len(pending), batch):
            chunk = pending[start:start + batch]
            payload = [{"id": n, "a": _desc(source, m.source_idx), "b": _desc(comp, m.comp_idx)} for n, m in enumerate(chunk)]
            try:
                out = client.generate_json(SYSTEM, "PAIRS:\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            except LLMError as exc:
                logger.warning("AI match check stopped: {}", exc)
                break
            for v in out.get("verdicts") or []:
                try:
                    verdicts[(chunk[int(v["id"])].source_idx, chunk[int(v["id"])].comp_idx)] = bool(v["same"])
                except (KeyError, ValueError, IndexError, TypeError):
                    continue
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
