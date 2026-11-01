"""Product-type classification, used to (1) group the pricing analysis ("Meat & Poultry", "Dairy & Eggs"...) and
(2) stop silly matches (an orange *juice* must not be matched to an orange *soda*, or a milk to a milk chocolate).

Rules are ordered: the FIRST matching rule wins, so specific product forms (juice, sauce, chocolate) come before the
ingredient they contain (orange, tomato, milk). Falls back to the shop's own category, then "Other"."""
from __future__ import annotations

import re

from src.scrapers.base import ProductRecord

# (category, regex of words/phrases). Matched against "title + category + tags", lower-cased.
_RULES: list[tuple[str, str]] = [
    # ---- phrases that contain a misleading ingredient word
    ("Meat & Poultry", r"soup meat|soup bones?|bone broth meat|stew meat|stewing"),
    ("Bags & Bottles", r"water bottle|shaker bottle|drink bottle|sports bottle|thermos"),
    ("Pantry & Staples", r"peanut butter|almond butter|nut butter|cream of tartar|coconut milk|coconut cream|soy milk powder|baking soda|baking powder|tomato paste|tomato puree|canned|tinned"),
    ("Snacks & Sweets", r"ice cream|choc(?:olate)?s?\b|candy|candies|gummy|gummies|lollipop|toffee|fudge|marshmallow|cookie|cookies|biscuit|biscuits|cracker|crackers|chips|crisps|popcorn|pretzel|snack|wafer|granola bar|protein bar|energy bar|nuts mix|trail mix|namkeen|bhujia|peanuts|almonds|cashews?|pistachio|walnuts?|raisins?|dried fruit"),
    # ---- drinks (before fruit/dairy words)
    ("Water", r"\b(?:mineral|spring|sparkling|artesian|drinking|distilled|alkaline)\s+water\b|\bwater\b(?!\s*melon)|\bseltzer\b"),
    ("Juices & Soft Drinks", r"juice|soda|soft drink|cola|coke|pepsi|sprite|fanta|lemonade|squash|cordial|energy drink|sports drink|iced tea|tonic|ginger ale|kool-aid|smoothie|nectar|drink mix|beverage"),
    ("Coffee & Tea", r"\bcoffee\b|\bespresso\b|\bcappuccino\b|\btea\b|teabag|tea bag|chai|matcha|cocoa powder|hot chocolate|creamer"),
    ("Alcohol", r"\bwine\b|\bbeer\b|\blager\b|\bale\b|\bcider\b|vodka|whisky|whiskey|\brum\b|\bgin\b|tequila|brandy|cognac|champagne|prosecco|moscato|pinot|merlot|chardonnay|cabernet|sauvignon|liqueur|spumante|\bsake\b|bourbon"),
    ("Breakfast & Cereal", r"cereal|cheerios|oats|oatmeal|muesli|cornflakes|corn flakes|pancake|waffle|porridge"),
    # ---- sauces, oils, condiments (before tomato/chilli/etc.)
    ("Sauces, Oils & Condiments", r"sauce|ketchup|catsup|mayo|mayonnaise|mustard|dressing|vinegar|\boil\b|\bghee\b|marinade|salsa|pickle|relish|chutney|hummus|dip\b|syrup|\bjam\b|jelly|honey|spread"),
    ("Bakery", r"\bbread\b|\bloaf\b|\bbun\b|\bbuns\b|bagel|croissant|muffin|\bcake\b|donut|doughnut|tortilla|\bpita\b|naan|roti|pastry|brioche|baguette|\brolls?\b"),
    ("Baking & Sweeteners", r"\bsugar\b|sweetener|splenda|stevia|\bflour\b|baking|yeast|custard|food colou?r|vanilla extract"),
    ("Pantry & Staples", r"\brice\b|pasta|spaghetti|macaroni|noodle|lentil|\bdal\b|beans?\b|chickpea|\bsoup\b|\bstock\b|bouillon|spice|masala|seasoning|\bsalt\b|pepper powder|couscous|quinoa|\bsemolina\b|tuna can|instant|ramen|mac & cheese|mac and cheese"),
    # ---- fresh categories
    ("Seafood", r"\bfish\b|salmon|\btuna\b|shrimp|prawn|\bcrab\b|lobster|\bcod\b|tilapia|sardine|squid|calamari|mackerel|snapper|mahi|grouper|haddock|\bhake\b|anchov|oyster|mussel|clam|seafood|fillet of fish|barramundi|\bbasa\b|pomfret|surmai"),
    ("Meat & Poultry", r"chicken|\bbeef\b|\bpork\b|\blamb\b|mutton|turkey|steak|sausage|bacon|\bham\b|salami|\bmince\b|minced|ground meat|ground beef|\bribs?\b|brisket|veal|\bgoat\b|\bduck\b|hot dog|frankfurter|drumstick|\bwings?\b|patty|patties|\bmeat\b|sirloin|ribeye|tenderloin|pepperoni|bratwurst|kebab|keema|cutlet|nugget|jerky|\bbone\b"),
    ("Dairy & Eggs", r"\bmilk\b|cheese|yogh?urt|\bbutter\b|\bcream\b|\beggs?\b|cheddar|mozzarella|parmesan|margarine|paneer|\bcurd\b|kefir|\bdahi\b|whipping|half & half|half and half|cottage|ricotta|feta|gouda|brie|lassi"),
    ("Fruits & Vegetables", r"apple|banana|orange|lemon|\blime\b|onion|potato|tomato|carrot|lettuce|cabbage|spinach|broccoli|cucumber|capsicum|bell pepper|garlic|ginger|mango|grape|avocado|berry|berries|strawberr|pineapple|vegetable|fruit|salad|mushroom|pumpkin|zucchini|courgette|eggplant|aubergine|cauliflower|celery|coriander|cilantro|parsley|mint|melon|papaya|guava|pomegranate|kiwi|peach|plum|pear|cherry|cherries|coconut|corn on|sweet potato|beetroot|radish|okra|peas\b|herbs?\b|\blemon bag"),
    ("Frozen Foods", r"\bfrozen\b|\bfrz\b"),
    # ---- non-food
    ("Headphones & Earbuds", r"headphone|earphone|earbud|ear bud|headset|\btws\b|airpod|neckband"),
    ("Speakers & Audio", r"speaker|soundbar|subwoofer|home theatre|boombox"),
    ("Watches & Wearables", r"smartwatch|smart watch|\bwatch\b|fitness band|fitness tracker|wearable"),
    ("Power & Charging", r"power bank|powerbank|charger|charging|adapter|adaptor|cable|\busb\b|battery|batteries"),
    ("Phones & Tablets", r"smartphone|\bphone\b|iphone|tablet|\bipad\b|android"),
    ("Computers & Accessories", r"laptop|notebook pc|keyboard|\bmouse\b|monitor|router|printer|webcam|hard drive|\bssd\b|pendrive|pen drive|flash drive|memory card"),
    ("TV & Cameras", r"\btv\b|television|camera|gopro|projector|drone"),
    ("Sports Bras & Tops", r"sports bra|tank top|\btank\b|crop top|\btee\b|t-shirt|tshirt|\bpolo\b|\bshirt\b|\btop\b|\bvest\b|singlet"),
    ("Bottoms", r"legging|tights?\b|shorts?\b|jogger|trouser|\bpants?\b|\bjeans\b|\bskirt\b|sweatpants"),
    ("Outerwear", r"hoodie|sweatshirt|jacket|\bcoat\b|\bparka\b|windbreaker|cardigan|\bzip\b"),
    ("Footwear", r"\bshoes?\b|sneaker|trainer|sandal|slipper|\bboots?\b|loafer|flip flop|flip-flop"),
    ("Bags & Bottles", r"backpack|\btote\b|\bbag\b|duffel|\bbottle\b|shaker|tumbler|flask"),
    ("Supplements", r"protein|whey|creatine|bcaa|vitamin|supplement|multivitamin|omega|collagen|pre-workout|probiotic"),
    ("Personal Care", r"shampoo|conditioner|soap|body wash|lotion|moisturi[sz]er|toothpaste|toothbrush|deodorant|perfume|cologne|razor|shaving|sunscreen|face wash|serum|lipstick|mascara|makeup|nail polish|hand wash|sanitizer|tampon|sanitary|diaper|wipes"),
    ("Household & Cleaning", r"detergent|dishwash|dish soap|cleaner|bleach|disinfect|tissue|toilet paper|paper towel|trash bag|garbage bag|air freshener|laundry|fabric softener|sponge|foil|cling wrap"),
    ("Baby", r"\bbaby\b|infant|formula|toddler|nappy|nappies"),
    ("Pet Supplies", r"\bdog\b|\bcat\b food|\bpuppy\b|\bkitten\b|pet food|cat litter"),
]
_COMPILED = [(cat, re.compile(rx, re.I)) for cat, rx in _RULES]

# Categories where a mismatch is a strong signal that two products are NOT the same thing.
CATEGORY_GROUP: dict[str, str] = {c: "Food & Grocery" for c in (
    "Pantry & Staples", "Snacks & Sweets", "Water", "Juices & Soft Drinks", "Coffee & Tea", "Alcohol", "Sauces, Oils & Condiments",
    "Breakfast & Cereal", "Bakery", "Baking & Sweeteners", "Frozen Foods", "Seafood", "Meat & Poultry", "Dairy & Eggs", "Fruits & Vegetables")}
CATEGORY_GROUP.update({c: "Electronics" for c in (
    "Headphones & Earbuds", "Speakers & Audio", "Watches & Wearables", "Power & Charging", "Phones & Tablets", "Computers & Accessories", "TV & Cameras")})
CATEGORY_GROUP.update({c: "Apparel & Sports" for c in ("Sports Bras & Tops", "Bottoms", "Outerwear", "Footwear", "Bags & Bottles", "Supplements")})
CATEGORY_GROUP.update({c: "Home & Care" for c in ("Personal Care", "Household & Cleaning", "Baby", "Pet Supplies")})

# Neighbouring categories that legitimately overlap on shops (one shop files bread rolls under Bakery, another under Frozen)
_COMPATIBLE: set[frozenset[str]] = {
    frozenset(p) for p in [
        ("Frozen Foods", "Meat & Poultry"), ("Frozen Foods", "Seafood"), ("Frozen Foods", "Fruits & Vegetables"),
        ("Frozen Foods", "Bakery"), ("Frozen Foods", "Snacks & Sweets"), ("Frozen Foods", "Pantry & Staples"),
        ("Pantry & Staples", "Sauces, Oils & Condiments"), ("Pantry & Staples", "Breakfast & Cereal"),
        ("Pantry & Staples", "Seafood"), ("Pantry & Staples", "Fruits & Vegetables"), ("Pantry & Staples", "Meat & Poultry"),
        ("Snacks & Sweets", "Breakfast & Cereal"), ("Snacks & Sweets", "Bakery"), ("Baking & Sweeteners", "Pantry & Staples"),
        ("Baking & Sweeteners", "Sauces, Oils & Condiments"), ("Coffee & Tea", "Juices & Soft Drinks"), ("Water", "Juices & Soft Drinks"),
        ("Dairy & Eggs", "Frozen Foods"), ("Dairy & Eggs", "Snacks & Sweets"), ("Dairy & Eggs", "Coffee & Tea"),
        ("Headphones & Earbuds", "Speakers & Audio"), ("Phones & Tablets", "Power & Charging"), ("Watches & Wearables", "Headphones & Earbuds"),
        ("Sports Bras & Tops", "Outerwear"), ("Bottoms", "Outerwear"), ("Personal Care", "Baby"), ("Household & Cleaning", "Personal Care"),
        ("Supplements", "Personal Care"), ("Pet Supplies", "Meat & Poultry"),
    ]
}


def _haystack(p: ProductRecord) -> str:
    return " ".join([p.title or "", p.category or "", " ".join(p.tags or [])]).lower()


def categorize_text(title: str, category: str | None = None, tags: list[str] | None = None) -> str | None:
    """Title is checked first (it names the actual product); the shop's category/tags are only a second opinion."""
    for text in (title.lower(), " ".join([category or "", " ".join(tags or [])]).lower()):
        if not text.strip():
            continue
        for cat, rx in _COMPILED:
            if rx.search(text):
                return cat
    return None


def infer_product_type(p: ProductRecord) -> str:
    """Product type for grouping: our taxonomy first, then the shop's own leaf category, then 'Other'."""
    hit = categorize_text(p.title or "", p.category, p.tags)
    if hit:
        return hit
    raw = (p.category or "").strip()
    if raw:
        leaf = [x.strip() for x in re.split(r"[>|/\\]", raw) if x.strip()]
        if leaf and leaf[-1].lower() not in {"other", "misc", "product", "products", "all", "sale", "new", "uncategorized", "uncategorised"} and len(leaf[-1]) > 2:
            return leaf[-1].title()
    return "Other"


def categories_compatible(a: str | None, b: str | None) -> bool:
    """True if two product types could plausibly describe the same item."""
    if not a or not b or a == b or "Other" in (a, b):
        return True
    if frozenset((a, b)) in _COMPATIBLE:
        return True
    ga, gb = CATEGORY_GROUP.get(a), CATEGORY_GROUP.get(b)
    if ga is None or gb is None:  # a shop-specific category we do not know: do not guess
        return True
    return False
