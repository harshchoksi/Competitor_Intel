"""Shared scraper types and helpers."""
from __future__ import annotations

import re
from abc import ABC, abstractmethod

from pydantic import BaseModel, Field

from src.config import AppConfig, SiteConfig
from src.scrapers.http import PoliteClient


class ScrapeError(Exception):
    """A scraper could not produce usable data for this site."""


class VariantRecord(BaseModel):
    external_id: str | None = None
    title: str | None = None
    sku: str | None = None
    price: float | None = None
    compare_at_price: float | None = None
    available: bool | None = None


class ProductRecord(BaseModel):
    external_id: str
    url: str | None = None
    title: str
    brand: str | None = None
    sku: str | None = None
    gtin: str | None = None
    mpn: str | None = None
    category: str | None = None
    image_url: str | None = None
    tags: list[str] = Field(default_factory=list)
    price: float | None = None
    compare_at_price: float | None = None  # only set when strictly greater than price
    currency: str | None = None
    in_stock: bool | None = None  # None = unknown (never guessed)
    low_stock: bool = False
    is_preorder: bool = False
    rating: float | None = None
    review_count: int | None = None
    variants: list[VariantRecord] = Field(default_factory=list)
    source_method: str = ""


_PRICE_RE = re.compile(r"[^0-9.]")


def parse_price(value) -> float | None:
    """'1,29,900.00' / '₹1299' / 1299 -> float. Returns None for missing, zero or unparseable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    cleaned = _PRICE_RE.sub("", str(value))
    if cleaned.count(".") > 1:
        head, _, tail = cleaned.rpartition(".")
        cleaned = head.replace(".", "") + "." + tail
    try:
        num = float(cleaned)
    except ValueError:
        return None
    return num if num > 0 else None


def valid_compare_at(compare_at: float | None, price: float | None) -> float | None:
    """A compare-at price only counts as a discount if it is above the selling price."""
    if compare_at is None or price is None or compare_at <= price:
        return None
    return compare_at


class BaseScraper(ABC):
    method: str = "base"

    def __init__(self, client: PoliteClient, site: SiteConfig, cfg: AppConfig) -> None:
        self.client = client
        self.site = site
        self.cfg = cfg
        self.origin = site.origin
        self.max_products = cfg.max_products_per_site
        self.currency = cfg.currency

    def wants(self, rec: ProductRecord) -> bool:
        """categories_to_track filter (substring match on title/category/tags/url). Empty = keep all."""
        terms = [t.lower() for t in self.cfg.categories_to_track if t.strip()]
        if not terms:
            return True
        haystack = " ".join([rec.title, rec.category or "", " ".join(rec.tags), rec.url or ""]).lower()
        return any(t in haystack for t in terms)

    @abstractmethod
    def scrape(self) -> list[ProductRecord]:
        """Return products or raise ScrapeError."""
