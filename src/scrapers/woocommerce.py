"""WooCommerce: public Store API (/wp-json/wc/store/v1/products)."""
from __future__ import annotations

from loguru import logger

from src.scrapers.base import BaseScraper, ProductRecord, ScrapeError, parse_price, valid_compare_at

PER_PAGE = 100


def _minor_to_major(value, minor_unit: int) -> float | None:
    if value in (None, ""):
        return None
    try:
        return parse_price(float(value) / (10 ** minor_unit))
    except (TypeError, ValueError):
        return None


def parse_woo_product(item: dict, currency_default: str) -> ProductRecord | None:
    title = (item.get("name") or "").strip()
    if not title or item.get("id") is None:
        return None
    prices = item.get("prices") or {}
    minor = int(prices.get("currency_minor_unit", 2) or 0)
    price = _minor_to_major(prices.get("price"), minor)
    regular = _minor_to_major(prices.get("regular_price"), minor)
    compare_at = valid_compare_at(regular, price) if item.get("on_sale") else None

    low = item.get("low_stock_remaining")
    reviews = item.get("review_count")
    rating = parse_price(item.get("average_rating"))
    cats = item.get("categories") or []
    brands = item.get("brands") or []
    images = item.get("images") or []

    return ProductRecord(
        external_id=str(item["id"]),
        url=item.get("permalink"),
        title=title,
        brand=(brands[0].get("name") if brands else None),
        sku=item.get("sku") or None,
        category=(cats[0].get("name") if cats else None),
        image_url=(images[0].get("src") if images else None),
        tags=[t.get("name") for t in (item.get("tags") or []) if t.get("name")],
        price=price,
        compare_at_price=compare_at,
        currency=prices.get("currency_code") or currency_default,
        in_stock=item.get("is_in_stock") if isinstance(item.get("is_in_stock"), bool) else None,
        low_stock=low is not None,
        is_preorder=bool(item.get("is_on_backorder")),
        rating=rating,
        review_count=int(reviews) if isinstance(reviews, (int, float)) else None,
        source_method="woocommerce_store_api",
    )


class WooCommerceScraper(BaseScraper):
    method = "woocommerce_store_api"

    def scrape(self) -> list[ProductRecord]:
        products: list[ProductRecord] = []
        page = 1
        while len(products) < self.max_products:
            res = self.client.get(
                f"{self.origin}/wp-json/wc/store/v1/products", params={"per_page": PER_PAGE, "page": page}
            )
            if res is None or not res.ok:
                if page == 1:
                    why = "unreachable or blocked" if res is None else f"HTTP {res.status_code}"
                    raise ScrapeError(f"WooCommerce Store API {why}")
                logger.warning("{}: stopped at page {} (fetch failed)", self.site.name, page)
                break
            items = res.json()
            if not isinstance(items, list):
                raise ScrapeError("WooCommerce Store API returned an unexpected payload")
            if not items:
                break
            for item in items:
                rec = parse_woo_product(item, self.currency)
                if rec and self.wants(rec):
                    products.append(rec)
                    if len(products) >= self.max_products:
                        break
            if len(items) < PER_PAGE:
                break
            page += 1
        return products
