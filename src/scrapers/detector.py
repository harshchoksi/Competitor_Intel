"""Platform detection: homepage markers + verifying probes of the public product feeds."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from loguru import logger

from src.scrapers.http import PoliteClient


class Platform(StrEnum):
    SHOPIFY = "shopify"
    WOOCOMMERCE = "woocommerce"
    MAGENTO = "magento"
    BIGCOMMERCE = "bigcommerce"
    CUSTOM = "custom"


@dataclass
class DetectionResult:
    platform: Platform
    reachable: bool = True
    feed_available: bool = False  # True only if the platform's public JSON feed actually responded
    evidence: list[str] = field(default_factory=list)


def _probe_shopify(client: PoliteClient, origin: str) -> bool:
    res = client.get(f"{origin}/products.json", params={"limit": 1})
    data = res.json() if res is not None and res.ok else None
    return isinstance(data, dict) and isinstance(data.get("products"), list)


def _probe_woo(client: PoliteClient, origin: str) -> bool:
    res = client.get(f"{origin}/wp-json/wc/store/v1/products", params={"per_page": 1})
    data = res.json() if res is not None and res.ok else None
    return isinstance(data, list)


def detect_platform(client: PoliteClient, url: str) -> DetectionResult:
    origin = url.rstrip("/")
    from urllib.parse import urlparse

    p = urlparse(origin)
    origin = f"{p.scheme}://{p.netloc}"

    home = client.get(origin)
    if home is None or home.status_code >= 400:
        reason = "unreachable or blocked by robots.txt" if home is None else f"HTTP {home.status_code}"
        logger.warning("{}: homepage {}", origin, reason)
        return DetectionResult(Platform.CUSTOM, reachable=False, evidence=[f"homepage {reason}"])

    html = home.text.lower()
    hdr = home.headers
    evidence: list[str] = []
    markers: dict[Platform, bool] = {
        Platform.SHOPIFY: "cdn.shopify.com" in html or "shopify.theme" in html or "x-shopid" in hdr,
        Platform.WOOCOMMERCE: "woocommerce" in html or "wp-content/plugins/woocommerce" in html,
        Platform.MAGENTO: "mage-cache-storage" in html or "magento_" in html or "/static/version" in html,
        Platform.BIGCOMMERCE: "cdn11.bigcommerce.com" in html or "bigcommerce" in hdr.get("x-powered-by", ""),
    }
    for plat, hit in markers.items():
        if hit:
            evidence.append(f"homepage marker: {plat.value}")

    if _probe_shopify(client, origin):
        return DetectionResult(Platform.SHOPIFY, feed_available=True, evidence=[*evidence, "/products.json responds"])
    if _probe_woo(client, origin):
        return DetectionResult(Platform.WOOCOMMERCE, feed_available=True, evidence=[*evidence, "Store API responds"])
    for plat in (Platform.SHOPIFY, Platform.WOOCOMMERCE, Platform.MAGENTO, Platform.BIGCOMMERCE):
        if markers[plat]:
            return DetectionResult(plat, feed_available=False, evidence=[*evidence, "public feed not available"])
    return DetectionResult(Platform.CUSTOM, evidence=evidence or ["no known platform markers"])
