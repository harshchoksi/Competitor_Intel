"""Pack-size parsing, so "Chicken Breast 500g", "Chicken Breast 0.5 kg" and "Chicken Breast 1.1 lb" are recognised as
the same size, and "Chicken Breast 1kg" is recognised as the same product in a bigger pack (compared per unit).

Only things whose price scales with size are treated as scalable: weight, volume and piece count.
Capacities such as 128GB/5000mAh are *not* scalable and are handled elsewhere as "spec" tokens."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

# unit -> (dimension, factor to base unit). Base units: gram, millilitre, piece.
_UNITS: dict[str, tuple[str, float]] = {
    "mg": ("mass", 0.001), "g": ("mass", 1.0), "gm": ("mass", 1.0), "gr": ("mass", 1.0), "gram": ("mass", 1.0), "grams": ("mass", 1.0),
    "kg": ("mass", 1000.0), "kgs": ("mass", 1000.0), "kilo": ("mass", 1000.0), "kilos": ("mass", 1000.0), "kilogram": ("mass", 1000.0), "kilograms": ("mass", 1000.0),
    "lb": ("mass", 453.592), "lbs": ("mass", 453.592), "pound": ("mass", 453.592), "pounds": ("mass", 453.592),
    "oz": ("mass", 28.3495), "ounce": ("mass", 28.3495), "ounces": ("mass", 28.3495),
    "ml": ("volume", 1.0), "cl": ("volume", 10.0), "dl": ("volume", 100.0),
    "l": ("volume", 1000.0), "lt": ("volume", 1000.0), "ltr": ("volume", 1000.0), "liter": ("volume", 1000.0), "litre": ("volume", 1000.0),
    "liters": ("volume", 1000.0), "litres": ("volume", 1000.0),
    "floz": ("volume", 29.5735), "gal": ("volume", 3785.41), "gallon": ("volume", 3785.41),
    "pc": ("count", 1.0), "pcs": ("count", 1.0), "piece": ("count", 1.0), "pieces": ("count", 1.0),
    "ct": ("count", 1.0), "count": ("count", 1.0), "pk": ("count", 1.0), "pack": ("count", 1.0), "packs": ("count", 1.0),
    "dozen": ("count", 12.0), "doz": ("count", 12.0), "sachet": ("count", 1.0), "sachets": ("count", 1.0),
    "bag": ("count", 1.0), "bags": ("count", 1.0), "can": ("count", 1.0), "cans": ("count", 1.0),
    "bottle": ("count", 1.0), "bottles": ("count", 1.0), "roll": ("count", 1.0), "rolls": ("count", 1.0),
}
_OZ_UNITS = {"oz", "ounce", "ounces", "floz"}
_NUM = r"(\d+(?:[.,]\d+)?)"
_UNIT_ALT = "|".join(sorted((re.escape(u) for u in _UNITS), key=len, reverse=True))
# "2 x 500g", "6x330ml", "500 g", "33cl", "8 fl oz", "12 pack", "6 pk"
_MULTI = re.compile(rf"(?<![\w.]){_NUM}\s*[x×]\s*{_NUM}\s*(fl\.?\s*oz|{_UNIT_ALT})(?![a-z])", re.I)
_SINGLE = re.compile(rf"(?<![\w.]){_NUM}\s*(fl\.?\s*oz|{_UNIT_ALT})(?![a-z])", re.I)
_PACK_OF = re.compile(r"(?:pack|box|case|set)\s+of\s+(\d+)\b", re.I)
_LIQUID_HINT = re.compile(r"\b(juice|water|soda|soft drink|drink|milk|oil|vinegar|wine|beer|cider|whisky|whiskey|vodka|rum|gin|liqueur|sauce|syrup|dressing|shampoo|lotion|detergent|cola|tea|coffee|smoothie|cream)\b", re.I)


@dataclass(frozen=True)
class Quantity:
    dim: str  # mass | volume | count
    base: float  # grams, millilitres or pieces in the whole pack
    label: str  # as shown to the user, e.g. "500 g"
    oz: float | None = None  # raw ounce count when the title said oz/fl oz (oz is used loosely for both)

    def unit_label(self) -> str:
        return {"mass": "100 g", "volume": "100 ml", "count": "piece"}[self.dim]


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def _fmt(n: float) -> str:
    return f"{n:g}" if n < 1000 else f"{n:,.0f}"


@lru_cache(maxsize=200_000)
def parse_quantity(title: str) -> Quantity | None:
    """Pack size found in a title, or None.

    Multipacks matter: "Coors Light 12oz, 24ct" is 24 x 12oz, NOT 12oz. Reading only the "12oz" made a 24-pack look like a single can,
    which produced absurd price gaps (+2000%). When a unit size and a pack count are both present they are multiplied."""
    if not title:
        return None
    text = title.replace("\u00a0", " ")
    best: Quantity | None = None

    def consider(q: Quantity | None) -> None:
        nonlocal best
        if q is not None and (best is None or q.base > best.base):
            best = q

    for m in _MULTI.finditer(text):  # "2 x 500g", "6x330ml": already a total
        n, each, unit = int(_num(m.group(1))), _num(m.group(2)), re.sub(r"[\s.]", "", m.group(3).lower())
        consider(_build(each * n, unit, text, f"{n} x {_fmt(each)} {m.group(3).strip()}"))
    if best is not None:
        return best
    measures: list[Quantity] = []
    counts: list[Quantity] = []
    for m in _SINGLE.finditer(text):
        value, unit = _num(m.group(1)), re.sub(r"[\s.]", "", m.group(2).lower())
        q = _build(value, unit, text, f"{_fmt(value)} {m.group(2).strip()}")
        if q is not None:
            (counts if q.dim == "count" else measures).append(q)
    m = _PACK_OF.search(text)
    if m and int(m.group(1)) > 0:
        counts.append(Quantity("count", float(m.group(1)), f"pack of {m.group(1)}"))
    if not counts and re.search(r"\bdozen\b", text, re.I):
        counts.append(Quantity("count", 12.0, "dozen"))
    unit_size = max(measures, key=lambda q: q.base) if measures else None
    pack = max(counts, key=lambda q: q.base) if counts else None
    if unit_size and pack and pack.base > 1:  # 12oz x 24 cans
        n = pack.base
        return Quantity(unit_size.dim, unit_size.base * n, f"{n:g} x {unit_size.label}", unit_size.oz * n if unit_size.oz else None)
    return unit_size or pack


def _build(value: float, unit: str, text: str, label: str) -> Quantity | None:
    if unit not in _UNITS or value <= 0:
        return None
    dim, factor = _UNITS[unit]
    oz = value if unit in _OZ_UNITS else None
    if unit == "oz" and _LIQUID_HINT.search(text):
        dim, factor = "volume", 29.5735  # 8 oz of dressing is fluid ounces
    return Quantity(dim, value * factor, label, oz)


def compare_quantities(a: Quantity | None, b: Quantity | None) -> tuple[str, float | None]:
    """-> (relation, ratio a/b).  relation: same | scalable | incompatible | unknown.

    'same'       equal within 3% (500 g vs 0.5 kg)
    'scalable'   same dimension, different size: comparable per unit (ratio within 0.2-5x)
    'incompatible' different dimensions or sizes too far apart to be the same product line"""
    if a is None or b is None:
        return "unknown", None
    if a.dim != b.dim:
        if a.oz and b.oz:  # "8 oz" (mass) vs "8 fl oz" (volume): the same label on the bottle
            ratio = a.oz / b.oz
            return ("same" if abs(ratio - 1) <= 0.03 else "scalable" if 0.2 <= ratio <= 5 else "incompatible"), ratio
        return "incompatible", None
    ratio = a.base / b.base
    if abs(ratio - 1) <= 0.03:
        return "same", 1.0
    if 0.2 <= ratio <= 5:
        return "scalable", ratio
    return "incompatible", ratio


def scale_price(comp_price: float, src: Quantity | None, comp: Quantity | None) -> tuple[float, bool]:
    """Competitor price expressed for *your* pack size. -> (price, was_adjusted)."""
    relation, ratio = compare_quantities(src, comp)
    if relation == "scalable" and ratio:
        return comp_price * ratio, True  # ratio = src/comp
    return comp_price, False


def strip_quantities(title: str) -> str:
    """The title with pack sizes ("2 x 500g", "33cl", "12 pack") cut out, so size never pollutes word matching."""
    t = _MULTI.sub(" ", title or "")
    t = _SINGLE.sub(" ", t)
    t = _PACK_OF.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def unit_price(price: float | None, q: Quantity | None) -> tuple[float, str] | None:
    """Price per 100 g / 100 ml / piece, so different pack sizes can be judged side by side."""
    if not price or q is None or q.base <= 0:
        return None
    if q.dim == "count":
        return price / q.base, "piece"
    return price / q.base * 100, "100 g" if q.dim == "mass" else "100 ml"
