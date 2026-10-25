"""Six fake stores on localhost:8101-8106 for trying the tool without touching live sites.

8101 Shopify (use as YOUR store) | 8102 WooCommerce | 8103 custom (JSON-LD) | 8104 Shopify | 8105 WooCommerce | 8106 custom
Prices, stock and discounts differ per store; a few products are missing from some stores.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# (sku, brand, base price INR, category)
CATALOG = [
    ("HP-100", "Sonex", 1999, "Headphones"), ("HP-200", "Sonex", 3499, "Headphones"),
    ("HP-300", "Auralis", 5999, "Headphones"), ("SP-100", "Auralis", 2499, "Speakers"),
    ("SP-200", "Boomer", 4499, "Speakers"), ("SP-300", "Boomer", 7999, "Speakers"),
    ("WT-100", "Chronos", 2999, "Watches"), ("WT-200", "Chronos", 6499, "Watches"),
    ("EB-100", "Sonex", 1299, "Earbuds"), ("EB-200", "Auralis", 2199, "Earbuds"),
    ("PB-100", "Voltix", 999, "Power banks"), ("PB-200", "Voltix", 1799, "Power banks"),
    ("CH-100", "Voltix", 699, "Chargers"), ("CH-200", "Voltix", 1299, "Chargers"),
]
SINGULAR = {"Headphones": "Headphone", "Speakers": "Speaker", "Watches": "Watch", "Earbuds": "Earbud", "Power banks": "Power bank", "Chargers": "Charger"}
STATE = {"epoch": 0}


def bump() -> int:
    """Simulate competitors changing prices, adding/removing products and starting a new offer."""
    STATE["epoch"] += 1
    return STATE["epoch"]


def reset() -> None:
    STATE["epoch"] = 0


PORTS = (8101, 8102, 8103, 8104, 8105, 8106)
KIND = {8101: "shopify", 8102: "woo", 8103: "custom", 8104: "shopify", 8105: "woo", 8106: "custom"}
FACTOR = {8101: 1.00, 8102: 0.93, 8103: 1.08, 8104: 0.97, 8105: 1.04, 8106: 0.89}
TITLES = {
    8101: "{b} {s} Wireless {c}", 8102: "{b} {c} Model {s}", 8103: "{b} {s} {c} (Black)",
    8104: "{s} {b} {c} Pro", 8105: "{b} {c} {s}", 8106: "{b} {s} {c} Edition",
}


def items(port: int):
    e = STATE["epoch"]
    catalog = list(CATALOG) + ([("CH-300", "Voltix", 1599, "Chargers")] if e and port == 8102 else [])
    for i, (sku, brand, base, cat) in enumerate(catalog):
        if port != 8101 and (i + port) % 9 == 0:
            continue  # this store doesn't carry it
        if e and port == 8103 and i == len(CATALOG) - 1:
            continue  # delisted after the change
        price = round(base * FACTOR[port] / 10) * 10 - 1
        if e and port != 8101 and i % 3 == 0:
            price = round(price * (1 - 0.08 * min(e, 3)))  # price cuts
        yield {
            "i": i, "sku": sku, "brand": brand, "cat": cat, "price": price,
            "list": round(price * (1.2 + 0.05 * (port % 4))) if (i + port) % 3 == 0 else None,
            "stock": (i + port) % 5 != 4, "low": (i * port) % 7 == 3,
            "title": TITLES[port].format(b=brand, s=sku, c=SINGULAR.get(cat, cat)),
        }


SHIP_THRESHOLD = {8101: 999, 8102: 499, 8103: 799, 8104: 1499, 8105: 599, 8106: 899}
PIXELS = {8101: ["fb"], 8102: ["fb", "gads"], 8103: [], 8104: ["fb", "gads", "tiktok"], 8105: ["gads"], 8106: ["fb"]}
SCRIPTS = {"fb": '<script src="https://connect.facebook.net/en_US/fbevents.js"></script>',
           "gads": '<script async src="https://www.googletagmanager.com/gtag/js?id=AW-1234567890"></script>',
           "tiktok": '<script src="https://analytics.tiktok.com/i18n/pixel/events.js"></script>'}


def home_html(port: int) -> str:
    e = STATE["epoch"]
    kind = KIND[port]
    marker = {"shopify": '<script src="https://cdn.shopify.com/s/x.js"></script>', "woo": "", "custom": ""}[kind]
    body_cls = ' class="woocommerce"' if kind == "woo" else ""
    pct = 10 + port % 10
    banner = f"Flat {pct}% OFF on audio. Use code SAVE{pct} at checkout"
    if e and port != 8101:
        banner += f" | Flash sale: extra 5% off with code FLASH{e}"
    return (f"<html><head>{marker}{''.join(SCRIPTS[x] for x in PIXELS[port])}</head><body{body_cls}>"
            f'<div class="announcement-bar">{banner}</div><div class="topbar">Free shipping on orders above ₹{SHIP_THRESHOLD[port]}</div>'
            f"<h1>Store {port}</h1><p>Pay with UPI, cards, EMI or Cash on Delivery.</p>"
            f'<footer><a href="/policies/refund">Refund policy</a><a href="https://www.instagram.com/store{port}/">Instagram</a>'
            f'<a href="https://www.facebook.com/store{port}page">Facebook</a><a href="https://www.youtube.com/@store{port}">YouTube</a></footer></body></html>')


def policy_html(port: int) -> str:
    return (f"<html><body><h1>Refund policy</h1><p>We offer a {7 if port % 2 else 14}-day easy return on unused items. "
            f"{1 if port % 3 else 2} year warranty on all products. Shipping charges are \u20b949 per order.</p></body></html>")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: str, ctype="text/html", status=200):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        port = self.server.server_port
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/robots.txt":
            return self._send(f"User-agent: *\nAllow: /\nSitemap: http://localhost:{port}/sitemap.xml\n", "text/plain")
        if u.path == "/":
            return self._send(home_html(port))
        if u.path == "/policies/refund":
            return self._send(policy_html(port))
        kind = KIND[port]
        if kind == "shopify":
            return self._shopify(u.path, q, port)
        if kind == "woo":
            return self._woo(u.path, q, port)
        return self._custom(u.path, port)

    def _shopify(self, path, q, port):
        if path == "/products.json":
            page, limit = int(q.get("page", [1])[0]), int(q.get("limit", [250])[0])
            rows = list(items(port))[(page - 1) * limit: page * limit]
            products = [{
                "id": 1000 + r["i"], "title": r["title"], "handle": r["sku"].lower(), "vendor": r["brand"],
                "product_type": r["cat"], "tags": ["bestseller"] if r["i"] % 4 == 0 else [],
                "variants": [{"id": 5000 + r["i"], "title": "Default", "sku": r["sku"], "price": f"{r['price']}.00",
                              "compare_at_price": f"{r['list']}.00" if r["list"] else None, "available": r["stock"]}],
                "images": [],
            } for r in rows]
            return self._send(json.dumps({"products": products}), "application/json")
        self._send("not found", status=404)

    def _woo(self, path, q, port):
        if path == "/wp-json/wc/store/v1/products":
            page, per = int(q.get("page", [1])[0]), int(q.get("per_page", [10])[0])
            rows = list(items(port))[(page - 1) * per: page * per]
            out = [{
                "id": 2000 + r["i"], "name": r["title"], "sku": r["sku"], "permalink": f"http://localhost:{port}/product/{r['sku'].lower()}/",
                "on_sale": bool(r["list"]), "is_in_stock": r["stock"], "low_stock_remaining": 3 if r["low"] and r["stock"] else None,
                "average_rating": "4.4" if r["i"] % 2 else "0", "review_count": 37 if r["i"] % 2 else 0,
                "categories": [{"name": r["cat"]}], "brands": [{"name": r["brand"]}], "images": [],
                "prices": {"price": str(r["price"] * 100), "regular_price": str((r["list"] or r["price"]) * 100),
                           "currency_code": "INR", "currency_minor_unit": 2},
            } for r in rows]
            return self._send(json.dumps(out), "application/json")
        self._send("not found", status=404)

    def _custom(self, path, port):
        rows = {r["sku"].lower(): r for r in items(port)}
        if path == "/sitemap.xml":
            urls = "".join(f"<url><loc>http://localhost:{port}/product/{k}</loc></url>" for k in rows)
            return self._send(f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}'
                              f"<url><loc>http://localhost:{port}/about</loc></url></urlset>", "application/xml")
        if path.startswith("/product/") and path.split("/")[-1] in rows:
            r = rows[path.split("/")[-1]]
            avail = "InStock" if r["stock"] else "OutOfStock"
            offer = {"@type": "Offer", "price": str(r["price"]), "priceCurrency": "INR", "availability": f"https://schema.org/{avail}"}
            if r["list"]:
                offer["priceSpecification"] = [{"@type": "UnitPriceSpecification", "price": str(r["list"]), "priceType": "https://schema.org/ListPrice"}]
            ld = {"@context": "https://schema.org", "@type": "Product", "name": r["title"], "sku": r["sku"],
                  "brand": {"@type": "Brand", "name": r["brand"]}, "category": r["cat"],
                  "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.1", "reviewCount": "52"}, "offers": offer}
            return self._send(f'<html><head><script type="application/ld+json">{json.dumps(ld)}</script></head><body>{r["title"]}</body></html>')
        self._send("not found", status=404)


def start_servers() -> list[ThreadingHTTPServer]:
    servers = [ThreadingHTTPServer(("127.0.0.1", p), Handler) for p in PORTS]
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    return servers


def main() -> None:
    start_servers()
    print("Mock stores running on :8101-:8106  (Ctrl+C to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
