from src.analysis.matching import match_products, model_tokens, norm_gtin, spec_tokens
from src.scrapers.base import ProductRecord


def P(title, brand=None, sku=None, gtin=None, price=100.0):
    return ProductRecord(external_id=title, title=title, brand=brand, sku=sku, gtin=gtin, price=price)


def pairs(ms):
    return {(m.source_idx, m.comp_idx): m for m in ms}


def test_tokens():
    assert model_tokens("Sonex HP-100 Wireless 128GB 5000mAh") == {"hp100"}
    assert model_tokens("Sony WH-1000XM5 Black") == {"wh1000xm5"}
    assert spec_tokens("iPhone 15 Pro 256GB") == {"15", "256gb"}
    assert norm_gtin("0890123456789") == "890123456789" and norm_gtin("123") is None


def test_gtin_beats_everything():
    m = match_products([P("Totally different words", gtin="8901234567890")], [P("Some other title", gtin="08901234567890")])
    assert m[0].method == "gtin" and m[0].confidence == 1.0 and m[0].status == "matched"


def test_sku_match():
    m = match_products([P("A thing", "Acme", sku="AC-9000")], [P("Another name", "Acme", sku="ac9000")])
    assert m[0].method == "sku" and m[0].status == "matched"


def test_model_number_match_across_different_wording():
    m = match_products([P("Sonex HP-100 Wireless Headphone", "Sonex")], [P("Sonex Headphone Model HP100", "Sonex")])
    assert m[0].method == "model" and m[0].status == "matched" and m[0].confidence >= 0.9


def test_conflicting_model_numbers_never_match():
    assert match_products([P("Sonex HP-100 Headphone", "Sonex")], [P("Sonex HP-200 Headphone", "Sonex")]) == []


def test_different_brands_never_match():
    assert match_products([P("Sonex HP-100 Headphone", "Sonex")], [P("Auralis HP-100 Headphone", "Auralis")]) == []


def test_spec_conflict_is_penalised_below_threshold():
    ms = match_products([P("Nova Phone 15 Pro 128GB", "Nova")], [P("Nova Phone 15 Pro 256GB", "Nova")])
    assert not ms or ms[0].status == "review"


def test_fuzzy_title_match_when_no_model_numbers():
    ms = match_products([P("Classic Cotton Crew Neck T-Shirt Blue", "Loom")], [P("Blue Classic Cotton Crew Neck T Shirt", "Loom")])
    assert ms and ms[0].method == "fuzzy" and ms[0].status == "matched"


def test_one_to_one_assignment():
    source = [P("Sonex HP-100 Headphone", "Sonex"), P("Sonex HP-100 Headphone Black", "Sonex")]
    comp = [P("Sonex Headphone HP-100", "Sonex")]
    ms = match_products(source, comp)
    assert len(ms) == 1  # the single competitor product is used once
