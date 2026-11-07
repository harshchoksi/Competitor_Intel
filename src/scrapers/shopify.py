"""Shopify: public /products.json feed (price, compare_at_price, variant availability)."""
from __future__ import annotations

from loguru import logger

from src.analysis.quantity import pick_size_hint, size_from_description
from src.scrapers.base import BaseScraper, ProductRecord, ScrapeError, VariantRecord, parse_price, valid_compare_at

PAGE_SIZE = 250


def parse_shopify_product(item: dict, origin: str, currency: str) -> ProductRecord | None:
    title = (item.get("title") or "").strip()
    if not title or item.get("id") is None:
        return None
    variants: list[VariantRecord] = []
    for v in item.get("variants") or []:
        price = parse_price(v.get("price"))
        variants.append(
            VariantRecord(
                external_id=str(v.get("id")) if v.get("id") is not None else None,
                title=v.get("title"),
                sku=(v.get("sku") or None),
                price=price,
                compare_at_price=valid_compare_at(parse_price(v.get("compare_at_price")), price),
                available=v.get("available"),
            )
        )
    priced = [v for v in variants if v.price is not None]
    pool = [v for v in priced if v.available] or priced
    rep = min(pool, key=lambda v: v.price) if pool else None  # type: ignore[arg-type,return-value]

    flags = [v.available for v in variants if v.available is not None]
    in_stock = any(flags) if flags else None

    tags = item.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    images = item.get("images") or []
    image = (images[0].get("src") if images else None) or (item.get("image") or {}).get("src")
    handle = item.get("handle")
    barcode = next((v.get("barcode") for v in item.get("variants") or [] if v.get("barcode")), None)

    raw = next((v for v in item.get("variants") or [] if rep is not None and str(v.get("id")) == str(rep.external_id)), None)
    if raw is None and item.get("variants"):
        raw = item["variants"][0]
    raw = raw or {}
    size_hint = pick_size_hint([raw.get("title"), raw.get("option1"), raw.get("option2"), raw.get("option3")]) or size_from_description(item.get("body_html"))
    grams = raw.get("grams")

    return ProductRecord(
        size_hint=size_hint, weight_grams=float(grams) if isinstance(grams, (int, float)) and grams > 0 else None,
        external_id=str(item["id"]),
        url=f"{origin}/products/{handle}" if handle else None,
        title=title,
        brand=item.get("vendor") or None,
        sku=next((v.sku for v in variants if v.sku), None),
        gtin=barcode,
        category=item.get("product_type") or None,
        image_url=image,
        tags=tags,
        price=rep.price if rep else None,
        compare_at_price=rep.compare_at_price if rep else None,
        currency=currency,
        in_stock=in_stock,
        variants=variants,
        source_method="shopify_products_json",
    )


class ShopifyScraper(BaseScraper):
    method = "shopify_products_json"

    def scrape(self) -> list[ProductRecord]:
        products: list[ProductRecord] = []
        seen_ids: set[str] = set()

        def fetch(page: int):
            return self.client.get(f"{self.origin}/products.json", params={"limit": PAGE_SIZE, "page": page}, rate=self.feed_rate())

        for page, res in self.paged(fetch):
            if len(products) >= self.max_products or self.out_of_time():
                break
            if res is None or not res.ok:
                if page == 1:
                    why = "unreachable or blocked" if res is None else f"HTTP {res.status_code}"
                    raise ScrapeError(f"/products.json {why}")
                logger.warning("{}: stopped at page {} (fetch failed)", self.site.name, page)
                break
            data = res.json()
            items = data.get("products") if isinstance(data, dict) else None
            if items is None:
                raise ScrapeError("/products.json returned an unexpected payload")
            if not items:
                break
            new_on_page = 0
            for item in items:
                rec = parse_shopify_product(item, self.origin, self.currency)
                if rec is None or rec.external_id in seen_ids:
                    continue
                seen_ids.add(rec.external_id)
                new_on_page += 1
                if self.wants(rec):
                    products.append(rec)
                    if len(products) >= self.max_products:
                        break
            self.report(len(products))
            if len(items) < PAGE_SIZE or new_on_page == 0:  # last page, or the site is repeating itself
                break
        return products
