"""Six fake grocery stores (~120 products each) on localhost:8101-8106 for trying the tool without touching live sites.

8101 Shopify (use as YOUR store) | 8102 WooCommerce | 8103 custom (JSON-LD) | 8104 Shopify | 8105 WooCommerce | 8106 custom
Every store words product names and pack sizes differently ("Chicken Breast 500g" / "CHICKEN BREAST FRESHFARM, 0.5 kg"),
prices differ per store, and a few products are missing from some stores - perfect for testing semantic matching.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


# ---- a ~120-product grocery catalogue, named differently by every store -------------------------------------------
# (name, brand, category, base price INR, size_value, size_unit)
def _build_catalog() -> list[tuple]:
    out: list[tuple] = []
    meat_base = {"Chicken": 260, "Beef": 520, "Pork": 340, "Lamb": 720, "Turkey": 410}
    cut_mult = {"Breast": 1.15, "Thighs": 0.95, "Mince": 1.0, "Ribs": 1.1, "Sausages": 0.9}
    for animal, base in meat_base.items():
        for cut, mult in cut_mult.items():
            for size_v, size_u in ((500, "g"), (1000, "g")):
                out.append((f"{animal} {cut}", "FreshFarm", "Meat", round(base * mult * size_v / 500), size_v, size_u))
    seafood = {"Salmon Fillet": 690, "Tiger Prawns": 560, "Tuna Steak": 480, "Basa Fillet": 280, "Crab Meat": 640}
    for name, base in seafood.items():
        for size_v in (250, 500):
            out.append((name, "OceanCatch", "Seafood", round(base * size_v / 500), size_v, "g"))
    dairy = [("Whole Milk", "Amul", 68, 1000, "ml"), ("Low Fat Milk", "Amul", 64, 1000, "ml"), ("Cheddar Cheese", "Britannia", 160, 200, "g"), ("Greek Yogurt", "Epigamia", 120, 400, "g"),
             ("Salted Butter", "Amul", 275, 500, "g"), ("Cream Cheese", "Philadelphia", 210, 200, "g"), ("Mozzarella Cheese", "Britannia", 190, 200, "g"), ("Paneer", "Amul", 95, 200, "g")]
    out += [(n, b, "Dairy", p, v, u) for n, b, p, v, u in dairy]
    drinks = [("Orange Juice", "Tropicana", 120, 1000, "ml"), ("Apple Juice", "Tropicana", 125, 1000, "ml"), ("Pineapple Juice", "Real", 110, 1000, "ml"), ("Mango Juice", "Real", 105, 1000, "ml"),
              ("Cola Soft Drink", "Coca-Cola", 40, 750, "ml"), ("Lemon Soda", "Sprite", 38, 750, "ml"), ("Sparkling Water", "Perrier", 130, 750, "ml"), ("Mineral Water", "Bisleri", 22, 1000, "ml"),
              ("Green Tea Bags", "Lipton", 210, 100, "pcs"), ("Instant Coffee", "Nescafe", 340, 200, "g")]
    out += [(n, b, "Drinks", p, v, u) for n, b, p, v, u in drinks]
    pantry = [("Basmati Rice", "India Gate", 780, 5000, "g"), ("White Sugar", "Madhur", 52, 1000, "g"), ("Spaghetti Pasta", "Barilla", 150, 500, "g"), ("Extra Virgin Olive Oil", "Figaro", 780, 1000, "ml"),
              ("Whole Wheat Flour", "Aashirvaad", 280, 5000, "g"), ("Tomato Ketchup", "Kissan", 160, 950, "g"), ("Peanut Butter Crunchy", "Sundrop", 245, 462, "g"), ("Honey", "Dabur", 220, 500, "g"),
              ("Corn Flakes Cereal", "Kelloggs", 230, 475, "g"), ("Rolled Oats", "Quaker", 205, 1000, "g"), ("Sunflower Oil", "Fortune", 190, 1000, "ml"), ("Iodised Salt", "Tata", 28, 1000, "g")]
    out += [(n, b, "Pantry", p, v, u) for n, b, p, v, u in pantry]
    produce = [("Tomatoes", 60), ("Onions", 45), ("Potatoes", 38), ("Carrots", 55), ("Bananas", 70), ("Apples", 190), ("Lemons", 95), ("Spinach", 40), ("Cucumber", 50), ("Eggplant", 65),
               ("Broccoli", 110), ("Capsicum", 120), ("Mangoes", 210), ("Grapes", 160), ("Garlic", 240)]
    for name, base in produce:
        for size_v in (500, 1000):
            out.append((name, "", "Produce", round(base * size_v / 1000), size_v, "g"))
    snacks = [("Potato Chips Salted", "Lays", 20, 52, "g"), ("Chocolate Cookies", "Britannia", 35, 120, "g"), ("Dark Chocolate Bar", "Amul", 110, 150, "g"), ("Salted Peanuts", "Haldirams", 60, 200, "g")]
    out += [(n, b, "Snacks", p, v, u) for n, b, p, v, u in snacks]
    return out


CATALOG = _build_catalog()
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
SYNONYM = {"Tiger Prawns": ["Tiger Prawns", "Shrimp Tiger", "Jumbo Prawn"], "Eggplant": ["Eggplant", "Aubergine", "Brinjal"], "Mince": ["Mince", "Ground", "Minced"], "Lemons": ["Lemons", "Lemon Bag", "Lemon"],
           "Cola Soft Drink": ["Cola Soft Drink", "Cola Drink", "Cola Soda"], "Thighs": ["Thighs", "Thigh", "Thigh Pieces"]}


def _size(v: int, u: str, style: int) -> str:
    """Same pack size, written differently by each store (500 g / 0.5 kg / 500gm / 1 lb-ish stays the same size)."""
    if u == "g":
        return [f"{v}g", f"{v / 1000:g} kg" if v >= 1000 or style == 1 else f"{v}g", f"{v} g", f"{v / 1000:g}kg", f"{v}gm", f"{v / 1000:g} KG"][style % 6]
    if u == "ml":
        return [f"{v}ml", f"{v / 1000:g} L" if v >= 1000 else f"{v}ml", f"{v} ml", f"{v / 1000:g}ltr" if v >= 1000 else f"{v}ml", f"{v}ml", f"{v / 10:g}cl"][style % 6]
    return f"{v} pcs" if style % 2 else f"{v} pack"


def _title(port: int, name: str, brand: str, v: int, u: str) -> str:
    for k, alts in SYNONYM.items():
        if k in name:
            name = name.replace(k, alts[port % len(alts)])
    size = _size(v, u, PORTS.index(port))
    style = PORTS.index(port) % 5
    if style == 0:
        return f"{brand} {name} {size}".strip()
    if style == 1:
        return f"{name.upper()} {brand.upper()}, {size}".replace(" ,", ",").strip()
    if style == 2:
        return f"{name} - {size} ({brand})" if brand else f"{name} - {size}"
    if style == 3:
        return f"{brand} {name}, {size}".strip(" ,")
    return f"Fresh {name} {size} {brand}".strip()


def items(port: int):
    e = STATE["epoch"]
    catalog = list(CATALOG) + ([("Chicken Wings", "FreshFarm", "Meat", 300, 500, "g")] if e and port == 8102 else [])
    for i, (name, brand, cat, base, v, u) in enumerate(catalog):
        if port != 8101 and (i * 7 + port) % 8 == 0:
            continue  # this store doesn't carry it
        if e and port == 8103 and i == len(CATALOG) - 1:
            continue  # delisted after the change
        noise = 1 + (((i * 31 + port * 17) % 13) - 6) / 100  # +/- 6% store-specific variation
        price = max(5, round(base * FACTOR[port] * noise))
        if e and port != 8101 and i % 3 == 0:
            price = round(price * (1 - 0.08 * min(e, 3)))  # price cuts
        sku = f"G-{i + 1:04d}" if port == 8101 else (f"{port % 100}{i + 1:05d}" if port in (8102, 8103) else f"X{i + 1:04d}")
        yield {
            "i": i, "sku": sku, "brand": brand, "cat": cat, "price": price,
            "list": round(price * (1.2 + 0.05 * (port % 4))) if (i + port) % 7 == 0 else None,
            "stock": (i + port) % 9 != 4, "low": (i * port) % 17 == 3,
            "title": _title(port, name, brand, v, u),
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
    banner = f"Flat {pct}% OFF on groceries. Use code SAVE{pct} at checkout"
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
