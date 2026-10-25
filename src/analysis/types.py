from __future__ import annotations

from dataclasses import dataclass, field

from src.scrapers.base import ProductRecord


@dataclass
class SiteData:
    key: str
    name: str
    is_source: bool
    status: str = "failed"  # ok | failed
    platform: str | None = None
    method: str | None = None
    error: str | None = None
    products: list[ProductRecord] = field(default_factory=list)
    signals: dict | None = None  # offers, shipping, policies, social links, marketing stack
    social: dict | None = None  # collected social stats (YouTube)
