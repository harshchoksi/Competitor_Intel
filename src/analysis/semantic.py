"""Semantic product matching.

Two shops rarely name a product the same way ("Soup Meat With Bone, 500g" vs "Soup Meat Bone In 0.5 kg"), so exact
keywords are not enough. A candidate pair is judged on several independent signals:

  * identifiers      barcode (GTIN), SKU and model numbers, when present
  * language         synonyms/plurals folded together (prawn=shrimp, yoghurt=yogurt, mince=ground, 12oz=340g...),
                     IDF-weighted token overlap (rare words such as the brand count for more than "fresh") and fuzzy text
  * meaning          optional sentence-embedding similarity (pip install -r requirements-semantic.txt)
  * logic guards     product type must be compatible; species/cut/diet/gender conflicts are rejected outright;
                     flavour/form/fat/preparation conflicts are penalised; different brands never match
  * size             same pack size is a plus; a different pack size still matches but is compared per unit

Everything here is deterministic and runs offline. The optional AI step lives in src/llm/match_verify.py."""
from __future__ import annotations

import math
import os
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from src.analysis.quantity import Quantity, compare_quantities, parse_quantity, strip_quantities
from src.analysis.taxonomy import categories_compatible, infer_product_type
from src.scrapers.base import ProductRecord

Embedder = Callable[[Sequence[str]], "object"]  # list[str] -> numpy array (n, d), rows L2-normalised

# --------------------------------------------------------------------------- text normalisation
_PHRASES: list[tuple[re.Pattern, str]] = [(re.compile(p, re.I), r) for p, r in [
    (r"\bsoft drinks?\b|\bfizzy drinks?\b|\bcold drinks?\b|\bpop\b", "soda"),
    (r"\bt[- ]?shirts?\b|\btees?\b", "tshirt"),
    (r"\bcapsicum\b", "bell pepper"),
    (r"\bmac(?:aroni)?\s*(?:&|and)\s*cheese\b|\bmac n cheese\b", "macaroni cheese"),
    (r"\bground meat\b", "mince"),
    (r"\bhot dogs?\b|\bfrankfurters?\b", "frankfurter"),
    (r"\bfrench fries\b|\bfries\b", "fries"),
    (r"\bsugar[- ]free\b|\bno sugar\b|\bzero sugar\b|\bzero\b|\bdiet\b|\blite\b", "diet"),
    (r"\bfull[- ]fat\b|\bfull[- ]cream\b|\bwhole milk\b", "fullfat milk"),
    (r"\blow[- ]fat\b|\breduced[- ]fat\b|\bsemi[- ]?skimmed\b|\bskimmed?\b|\bskim\b|\bnon[- ]?fat\b", "lowfat"),
    (r"\bbone[- ]in\b|\bwith bone\b|\bbone in\b", "bonein"),
    (r"\bboneless\b|\bwithout bone\b|\bdeboned\b", "boneless"),
    (r"\bgluten[- ]free\b", "glutenfree"),
    (r"\blactose[- ]free\b", "lactosefree"),
    (r"\bearbuds?\b|\bear buds?\b", "earphone"),
    (r"\bcell ?phone\b|\bmobile phone\b|\bmobile\b", "phone"),
]]
_SYN = {
    "yoghurt": "yogurt", "chilli": "chili", "chillies": "chili", "chilies": "chili", "aubergine": "eggplant", "brinjal": "eggplant",
    "courgette": "zucchini", "coriander": "cilantro", "prawn": "shrimp", "prawns": "shrimp", "mince": "ground", "minced": "ground",
    "colour": "color", "flavour": "flavor", "flavoured": "flavored", "flavored": "flavored", "aluminium": "aluminum", "doughnut": "donut",
    "doughnuts": "donut", "crisps": "chip", "biscuit": "cookie", "biscuits": "cookie", "choc": "chocolate", "mayo": "mayonnaise",
    "catsup": "ketchup", "whisky": "whiskey", "veg": "vegetable", "filet": "fillet", "trainer": "sneaker", "trainers": "sneaker",
    "tomatoe": "tomato", "potatoe": "potato", "mutton": "lamb",
    "papad": "papadum", "pappad": "papadum", "curd": "yogurt", "dahi": "yogurt", "atta": "flour", "maida": "flour",
}
_STOP = {"the", "a", "an", "of", "and", "with", "for", "by", "in", "on", "to", "from", "new", "each", "per", "approx", "assorted", "pack", "pk",
         "ct", "pcs", "pc", "size", "only", "style", "type", "brand", "premium", "quality", "select", "selected", "original"}
_NON_ALNUM = re.compile(r"[^a-z0-9 ]")


def ascii_lower(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def stem(w: str) -> str:
    if len(w) <= 3 or any(c.isdigit() for c in w):
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("sses", "xes", "zes", "ches", "shes", "oes")):
        return w[:-2]
    if w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def prepare_text(title: str) -> str:
    """Lower-case, ASCII, quantities removed, phrase synonyms applied. Keeps hyphens/spaces for attribute detection."""
    t = strip_quantities(ascii_lower(title)).replace("&", " and ")
    for rx, rep in _PHRASES:
        t = rx.sub(rep, t)
    return t


def tokenize(prepared: str) -> tuple[str, ...]:
    out: list[str] = []
    for w in _NON_ALNUM.sub(" ", prepared).split():
        if w in _STOP:
            continue
        w = stem(_SYN.get(w, w))
        w = _SYN.get(w, w)
        if w:
            out.append(w)
    return tuple(out)


# --------------------------------------------------------------------------- attribute groups
# hard=True: a clear conflict (both sides name a value, and the values differ) rejects the pair.
# asym_hard=True: a value on only one side also rejects (Diet Cola is not Cola).
@dataclass(frozen=True)
class AttrGroup:
    name: str
    values: dict[str, str]  # value -> regex
    hard: bool = False
    asym_hard: bool = False
    conflict_penalty: float = 0.5
    one_sided_penalty: float = 1.0

    def compiled(self) -> list[tuple[str, re.Pattern]]:
        return [(v, re.compile(rx, re.I)) for v, rx in self.values.items()]

    def any_rx(self) -> re.Pattern:
        return re.compile("|".join(f"(?:{rx})" for rx in self.values.values()), re.I)


_GROUPS: list[AttrGroup] = [
    AttrGroup("species", {
        "chicken": r"\bchicken\b|\bhen\b|\bbroiler\b", "beef": r"\bbeef\b|\bveal\b|\bsirloin\b|\bribeye\b|\bbrisket\b", "pork": r"\bpork\b|\bbacon\b|\bham\b|\bsalami\b",
        "lamb": r"\blamb\b|\bmutton\b|\bgoat\b", "turkey": r"\bturkey\b", "duck": r"\bduck\b", "salmon": r"\bsalmon\b", "tuna": r"\btuna\b",
        "shrimp": r"\bshrimps?\b|\bprawns?\b", "cod": r"\bcod\b", "tilapia": r"\btilapia\b", "crab": r"\bcrab\b", "lobster": r"\blobster\b",
        "sardine": r"\bsardines?\b", "egg": r"\beggs?\b",
    }, hard=True),
    AttrGroup("diet", {"diet": r"\bdiet\b|\bsugarless\b|\bsugar free\b|\bno calories\b|\bcalorie free\b"}, hard=True, asym_hard=True),
    AttrGroup("audience", {
        "men": r"\bmen'?s?\b|\bmale\b|\bgents?\b|\bman\b", "women": r"\bwomen'?s?\b|\bfemale\b|\bladies\b|\bwoman\b|\bgirls?\b",
        "kids": r"\bkids?\b|\bboys?\b|\bjunior\b|\bchildren\b|\bchild\b|\binfant\b|\btoddler\b|\byouth\b",
    }, hard=True),
    AttrGroup("cut", {
        "breast": r"\bbreasts?\b", "thigh": r"\bthighs?\b", "wing": r"\bwings?\b", "drumstick": r"\bdrumsticks?\b|\bleg quarters?\b", "leg": r"\blegs?\b",
        "ribs": r"\bribs?\b", "loin": r"\bloin\b|\btenderloin\b", "ground": r"\bground\b|\bmince\b|\bkeema\b", "fillet": r"\bfillets?\b|\bfilets?\b",
        "steak": r"\bsteaks?\b", "shoulder": r"\bshoulder\b", "belly": r"\bbelly\b", "liver": r"\bliver\b", "neck": r"\bneck\b", "whole": r"\bwhole (?:chicken|fish|turkey|duck)\b",
        "sausage": r"\bsausages?\b", "nugget": r"\bnuggets?\b", "patty": r"\bpatt(?:y|ies)\b",
    }, hard=True, one_sided_penalty=0.93),
    AttrGroup("prep", {"bonein": r"\bbonein\b|\bbone in\b", "boneless": r"\bboneless\b", "skinless": r"\bskinless\b", "skinon": r"\bskin[- ]on\b"}, conflict_penalty=0.55, one_sided_penalty=0.94),
    AttrGroup("form", {
        "frozen": r"\bfrozen\b", "canned": r"\bcanned\b|\btinned\b|\bin brine\b|\bin oil\b", "dried": r"\bdried\b|\bdehydrated\b", "powder": r"\bpowder(?:ed)?\b",
        "smoked": r"\bsmoked\b", "cooked": r"\bcooked\b|\bready to eat\b|\broasted\b", "instant": r"\binstant\b", "liquid": r"\bliquid\b",
    }, conflict_penalty=0.55, one_sided_penalty=0.97),
    AttrGroup("fat", {"lowfat": r"\blowfat\b", "fullfat": r"\bfullfat\b", "twopct": r"\b2 ?%|\b2 percent\b", "onepct": r"\b1 ?%|\b1 percent\b"}, conflict_penalty=0.5, one_sided_penalty=0.95),
    AttrGroup("flavor", {
        "vanilla": r"\bvanilla\b", "chocolate": r"\bchocolate\b|\bcocoa\b", "strawberry": r"\bstrawberry\b|\bstrawberries\b", "mango": r"\bmango\b", "orange": r"\borange\b",
        "lemon": r"\blemon\b|\blime\b|\blemon-lime\b", "apple": r"\bapple\b", "grape": r"\bgrapes?\b", "pineapple": r"\bpineapple\b", "cherry": r"\bcherry\b|\bcherries\b",
        "peach": r"\bpeach\b", "banana": r"\bbanana\b", "coconut": r"\bcoconut\b", "mint": r"\bmint\b|\bspearmint\b|\bpeppermint\b", "honey": r"\bhoney\b", "caramel": r"\bcaramel\b",
        "coffee": r"\bcoffee\b|\bmocha\b", "bbq": r"\bbbq\b|\bbarbecue\b|\bbarbeque\b", "ranch": r"\branch\b", "cheese": r"\bcheddar\b|\bcheesy\b|\bnacho\b",
        "spicy": r"\bspicy\b|\bhot & spicy\b|\bchili\b|\bjalapeno\b", "salted": r"\bsalted\b|\bsea salt\b", "berry": r"\bblueberry\b|\braspberry\b|\bmixed berry\b|\bblackberry\b",
        "watermelon": r"\bwatermelon\b", "passion": r"\bpassion fruit\b|\bguava\b|\blychee\b", "tomato": r"\btomato\b", "garlic": r"\bgarlic\b", "olive": r"\bolive\b|\bextra virgin\b",
        "sesame": r"\bsesame\b", "almond": r"\balmond\b", "peanut": r"\bpeanut\b|\bgroundnut\b", "hazelnut": r"\bhazelnut\b",
    }, conflict_penalty=0.5, one_sided_penalty=0.92),
    AttrGroup("claims", {"organic": r"\borganic\b", "glutenfree": r"\bglutenfree\b", "lactosefree": r"\blactosefree\b", "vegan": r"\bvegan\b", "wholegrain": r"\bwhole ?grain\b|\bwholemeal\b|\bwhole wheat\b|\bbrown\b"},
              conflict_penalty=0.9, one_sided_penalty=0.95),
]
_GROUPS_C = [(g, g.compiled()) for g in _GROUPS]
_GROUP_ANY = {g.name: g.any_rx() for g in _GROUPS}


def extract_attrs(prepared: str) -> dict[str, frozenset[str]]:
    out: dict[str, frozenset[str]] = {}
    for g, pats in _GROUPS_C:
        if not _GROUP_ANY[g.name].search(prepared):  # most titles mention none of a group's words: one search instead of one per value
            continue
        found = frozenset(v for v, rx in pats if rx.search(prepared))
        if found:
            out[g.name] = found
    return out


def attribute_factor(a: dict[str, frozenset[str]], b: dict[str, frozenset[str]]) -> tuple[float, list[str]]:
    """-> (multiplier, notes). multiplier 0 = reject."""
    factor, notes = 1.0, []
    for g, _ in _GROUPS_C:
        va, vb = a.get(g.name), b.get(g.name)
        if va and vb:
            if g.hard and not (va & vb):
                return 0.0, [f"{g.name} differs ({'/'.join(sorted(va))} vs {'/'.join(sorted(vb))})"]
            if not g.hard and va != vb and not (va & vb):
                factor *= g.conflict_penalty
                notes.append(f"{g.name} differs ({'/'.join(sorted(va))} vs {'/'.join(sorted(vb))})")
            elif va != vb and not g.hard:
                factor *= 0.9
        elif va or vb:
            if g.asym_hard:
                return 0.0, [f"only one is {g.name}"]
            if g.one_sided_penalty < 1:
                factor *= g.one_sided_penalty
                notes.append(f"{g.name} only on one side")
    return factor, notes


# --------------------------------------------------------------------------- identifiers (kept compatible with the original matcher)
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[-/][A-Za-z0-9]+)*")
_UNIT_RE = re.compile(r"^\d+(\.\d+)?(gb|tb|mb|mah|w|mm|cm|inch|in|kg|kgs|g|gm|ml|l|ltr|hz|ghz|mp|v|x|pcs|pc|pack|oz|lb|lbs|cl|dl|ct|floz)$", re.I)
_SCALABLE_UNIT_RE = re.compile(r"^\d+(\.\d+)?(kg|kgs|g|gm|ml|l|ltr|pcs|pc|pack|oz|lb|lbs|cl|dl|ct|floz)$", re.I)
_ALNUM = re.compile(r"[^a-z0-9]")


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


def fixed_spec_tokens(title_without_quantities: str) -> set[str]:
    """Specs that do not scale with price (capacities, model years...). Pack sizes were already removed."""
    return {t for t in spec_tokens(title_without_quantities) if not _SCALABLE_UNIT_RE.match(t)}


# --------------------------------------------------------------------------- profiles
@dataclass
class Profile:
    idx: int
    title: str
    prepared: str
    tokens: tuple[str, ...]
    token_set: frozenset[str]
    text: str  # tokens joined, for fuzzy matching
    brand: str  # normalised, "" if unknown/unreliable
    brand_in_title: bool
    gtin: str | None
    sku: str | None
    models: set[str]
    specs: set[str]
    qty: Quantity | None
    attrs: dict[str, frozenset[str]]
    ptype: str
    category_raw: str | None = None
    extra: dict = field(default_factory=dict)


def _site_store_brands(products: Sequence[ProductRecord]) -> set[str]:
    """A 'brand' (Shopify vendor) shared by most of a shop's catalogue is the shop's own name, not a real brand."""
    counts = Counter(_norm(p.brand) for p in products if p.brand)
    n = len(products)
    return {b for b, c in counts.items() if b and n >= 12 and c / n > 0.5}


def build_profiles(products: Sequence[ProductRecord]) -> list[Profile]:
    store_brands = _site_store_brands(products)
    out: list[Profile] = []
    for i, p in enumerate(products):
        prepared = prepare_text(p.title)
        tokens = tokenize(prepared)
        bn = _norm(p.brand)
        if bn in store_brands:
            bn = ""
        title_norm = _norm(p.title)
        in_title = bool(bn) and bn in title_norm
        stripped = strip_quantities(p.title)
        out.append(Profile(
            idx=i, title=p.title, prepared=prepared, tokens=tokens, token_set=frozenset(tokens), text=" ".join(tokens), brand=bn, brand_in_title=in_title,
            gtin=norm_gtin(p.gtin), sku=norm_sku(p.sku) or norm_sku(p.mpn), models=model_tokens(stripped), specs=fixed_spec_tokens(stripped),
            qty=parse_quantity(p.title), attrs=extract_attrs(prepared), ptype=infer_product_type(p), category_raw=p.category,
        ))
    return out


# --------------------------------------------------------------------------- scoring
@dataclass(frozen=True)
class Scored:
    conf: float
    method: str
    title_sim: float
    size_relation: str  # same | scalable | unknown | incompatible
    size_ratio: float | None  # your size / their size
    reasons: tuple[str, ...]


def _brand_state(a: Profile, b: Profile) -> str:
    """same | conflict | weak_conflict | unknown.  A hard conflict needs both brands to look real (each appears in its own title)."""
    if not a.brand or not b.brand:
        return "unknown"
    if a.brand == b.brand or a.brand in b.brand or b.brand in a.brand:
        return "same"
    if a.brand in _norm(b.title) or b.brand in _norm(a.title):
        return "same"  # one shop files the brand only in the product name
    if a.brand_in_title and b.brand_in_title:
        return "conflict"
    return "weak_conflict"


def _idf(src: Sequence[Profile], comp: Sequence[Profile]) -> dict[str, float]:
    df: Counter[str] = Counter()
    for pr in (*src, *comp):
        df.update(pr.token_set)
    n = len(src) + len(comp)
    return {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}


def _tfidf_cosine(a: Profile, b: Profile, idf: dict[str, float]) -> float:
    """IDF-weighted cosine, with credit for containment: "Basmati Rice" inside "Long Grain Basmati Rice" is a good sign,
    even though the extra words dilute the cosine. Containment needs >= 2 words on the shorter side."""
    if not a.token_set or not b.token_set:
        return 0.0
    shared = a.token_set & b.token_set
    num = sum(idf[t] ** 2 for t in shared)
    da = math.sqrt(sum(idf[t] ** 2 for t in a.token_set))
    db = math.sqrt(sum(idf[t] ** 2 for t in b.token_set))
    cos = num / (da * db) if da and db else 0.0
    if min(len(a.token_set), len(b.token_set)) >= 2:
        wa, wb = sum(idf[t] for t in a.token_set), sum(idf[t] for t in b.token_set)
        coverage = sum(idf[t] for t in shared) / min(wa, wb)
        cos = max(cos, 0.85 * coverage)
    return cos


def _fuzzy(a: str, b: str) -> float:
    return (fuzz.token_set_ratio(a, b) + fuzz.token_sort_ratio(a, b)) / 200.0


def _emb_rescale(cos: float) -> float:
    return max(0.0, min(1.0, (cos - 0.40) / 0.50))


def score_pair(a: Profile, b: Profile, idf: dict[str, float], emb_cos: float | None, review_floor: float) -> Scored | None:
    """Judge one (your product, their product) pair. None = definitely not the same product."""
    brand = _brand_state(a, b)
    if brand == "conflict":
        return None
    reasons: list[str] = []
    lex = _tfidf_cosine(a, b, idf)
    fz = _fuzzy(a.text, b.text)
    if emb_cos is not None:
        base = 0.40 * lex + 0.20 * fz + 0.40 * _emb_rescale(emb_cos)
        method = "semantic"
    else:
        base = 0.60 * lex + 0.40 * fz
        method = "fuzzy"
    title_sim = base
    conf = base * 0.95

    af, notes = attribute_factor(a.attrs, b.attrs)
    if af == 0.0:
        return None
    conf *= af
    reasons.extend(notes)

    if not categories_compatible(a.ptype, b.ptype):
        conf *= 0.75
        reasons.append(f"different product types ({a.ptype} vs {b.ptype})")
    if brand == "same" and a.brand:
        conf = min(0.99, conf + 0.02)
        reasons.insert(0, "same brand")
    elif brand == "weak_conflict":
        conf *= 0.9
        reasons.append("brands differ")

    relation, ratio = compare_quantities(a.qty, b.qty)
    if relation == "same":
        conf = min(0.99, conf + 0.03)
        reasons.append(f"same pack size ({a.qty.label})")  # type: ignore[union-attr]
    elif relation == "scalable":
        conf *= 0.95
        reasons.append(f"different pack size ({a.qty.label} vs {b.qty.label}), compared per unit")  # type: ignore[union-attr]
    elif relation == "incompatible":
        conf *= 0.6
        reasons.append("pack sizes are not comparable")
    elif (a.qty is None) != (b.qty is None):
        conf *= 0.93  # one listing states its pack size, the other does not: cannot be verified
        reasons.append("pack size not stated on one side")

    if a.specs and b.specs and a.specs != b.specs:
        conf *= 0.85
        reasons.append("specifications differ")
    if conf < review_floor:
        return None
    reasons.append(f"title similarity {round(title_sim * 100)}%")
    return Scored(round(min(conf, 0.99), 3), method, round(title_sim, 3), relation, ratio, tuple(reasons))


# --------------------------------------------------------------------------- candidate generation + assignment
def _candidates_lexical(src: Sequence[Profile], comp: Sequence[Profile], idf: dict[str, float], top_k: int) -> dict[int, set[int]]:
    """For each competitor product the source products sharing its most informative words (inverted index, no N x M scan).

    Same scoring as a plain Python loop (sum of the IDF of shared words, very common words ignored), but accumulated with numpy:
    about 5x faster on catalogues of thousands of products, with identical candidates."""
    import numpy as np

    n = len(src)
    postings: dict[str, list[int]] = defaultdict(list)
    for p in src:
        for t in p.token_set:
            postings[t].append(p.idx)
    max_df = max(60, int(0.25 * n))
    arrays = {t: np.asarray(v, dtype=np.int64) for t, v in postings.items() if len(v) <= max_df}
    out: dict[int, set[int]] = {}
    for c in comp:
        parts = [(arrays[t], idf.get(t, 1.0)) for t in c.token_set if t in arrays]
        if not parts:
            out[c.idx] = set()
            continue
        idx = np.concatenate([a for a, _ in parts])
        w = np.concatenate([np.full(len(a), wt) for a, wt in parts])
        acc = np.bincount(idx, weights=w, minlength=n)
        hits = np.flatnonzero(acc)
        if len(hits) > top_k:
            hits = hits[np.argpartition(-acc[hits], top_k - 1)[:top_k]]
        out[c.idx] = {int(i) for i in hits}
    return out


def _candidates_embedding(src_vecs, comp_vecs, top_k: int) -> dict[int, set[int]]:
    import numpy as np

    out: dict[int, set[int]] = {}
    step = 512
    for start in range(0, comp_vecs.shape[0], step):
        sims = comp_vecs[start:start + step] @ src_vecs.T
        k = min(top_k, sims.shape[1])
        top = np.argpartition(-sims, k - 1, axis=1)[:, :k] if k < sims.shape[1] else np.tile(np.arange(sims.shape[1]), (sims.shape[0], 1))
        for r in range(sims.shape[0]):
            out[start + r] = {int(i) for i in top[r]}
    return out


def semantic_match(
    src: Sequence[Profile], comp: Sequence[Profile], *, threshold: float, review_floor: float, embedder: Embedder | None = None, top_k: int = 12,
) -> list[tuple[int, int, Scored, str]]:
    """-> [(source_idx, comp_idx, scored, status)], one-to-one, best pairs first."""
    if not src or not comp:
        return []
    idf = _idf(src, comp)
    cand = _candidates_lexical(src, comp, idf, top_k)
    sv = cv = None
    if embedder is not None:
        try:
            sv = embedder([f"{p.brand} {p.text}".strip() for p in src])
            cv = embedder([f"{p.brand} {p.text}".strip() for p in comp])
            for cj, ids in _candidates_embedding(sv, cv, top_k).items():
                cand.setdefault(cj, set()).update(ids)
        except Exception:  # an embedding failure must never break matching; fall back to lexical only
            sv = cv = None

    offers: dict[tuple[int, int], Scored] = {}

    def offer(i: int, j: int, s: Scored) -> None:
        cur = offers.get((i, j))
        if cur is None or s.conf > cur.conf:
            offers[(i, j)] = s

    s_gtin = {p.gtin: p.idx for p in src if p.gtin}
    s_sku: dict[str, list[int]] = defaultdict(list)
    for p in src:
        if p.sku:
            s_sku[p.sku].append(p.idx)

    for c in comp:
        # 1. barcode
        if c.gtin and c.gtin in s_gtin:
            i = s_gtin[c.gtin]
            offer(i, c.idx, Scored(1.0, "gtin", 1.0, compare_quantities(src[i].qty, c.qty)[0], compare_quantities(src[i].qty, c.qty)[1], ("same barcode",)))
        # 2. SKU / MPN (numeric-only SKUs collide between shops, so they need some textual agreement)
        for i in s_sku.get(c.sku or "", []):
            a = src[i]
            if _brand_state(a, c) == "conflict":
                continue
            if not any(ch.isalpha() for ch in c.sku or "") and _fuzzy(a.text, c.text) < 0.35:
                continue
            rel, ratio = compare_quantities(a.qty, c.qty)
            offer(i, c.idx, Scored(0.97, "sku", round(_fuzzy(a.text, c.text), 3), rel, ratio, ("same SKU",)))
        # 3. model numbers and the semantic score for the shortlisted candidates
        ids = set(cand.get(c.idx, set()))
        for i in ids:
            a = src[i]
            if _brand_state(a, c) == "conflict":
                continue
            shared = a.models & c.models
            if shared:
                fz = _fuzzy(a.text, c.text)
                if (a.brand and c.brand) or fz >= 0.6:
                    rel, ratio = compare_quantities(a.qty, c.qty)
                    offer(i, c.idx, Scored(round(0.90 + 0.06 * fz, 3), "model", round(fz, 3), rel, ratio, (f"same model number {sorted(shared)[0].upper()}",)))
                continue
            if a.models and c.models:  # both name a model, but different ones -> different products
                continue
            cos = float(sv[i] @ cv[c.idx]) if sv is not None and cv is not None else None
            s = score_pair(a, c, idf, cos, review_floor)
            if s is not None:
                offer(i, c.idx, s)

    out: list[tuple[int, int, Scored, str]] = []
    used_i: set[int] = set()
    used_j: set[int] = set()
    for (i, j), s in sorted(offers.items(), key=lambda kv: -kv[1].conf):
        if i in used_i or j in used_j:
            continue
        used_i.add(i)
        used_j.add(j)
        out.append((i, j, s, "matched" if s.conf >= threshold else "review"))
    return out


# --------------------------------------------------------------------------- optional embedder
_EMBEDDER_CACHE: dict[str, Embedder | None] = {}


def load_embedder() -> Embedder | None:
    """Sentence-transformer embedder if installed (requirements-semantic.txt) and not disabled; otherwise None.
    Set SEMANTIC_EMBEDDINGS=off to disable, SEMANTIC_MODEL to choose the model."""
    if os.environ.get("SEMANTIC_EMBEDDINGS", "auto").lower() in ("off", "0", "false", "no"):
        return None
    model_name = os.environ.get("SEMANTIC_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    if model_name in _EMBEDDER_CACHE:
        return _EMBEDDER_CACHE[model_name]
    emb: Embedder | None = None
    try:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(model_name)

        def emb(texts: Sequence[str]):  # noqa: F811
            return model.encode(list(texts), batch_size=128, normalize_embeddings=True, show_progress_bar=False)
    except Exception:
        emb = None
    _EMBEDDER_CACHE[model_name] = emb
    return emb
