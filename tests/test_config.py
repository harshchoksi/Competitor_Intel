import pytest

from src.config import AppConfig, ConfigError, SiteConfig, load_config


def test_url_normalised():
    assert SiteConfig(name="A", url="Shop.example.com/").url == "https://Shop.example.com"
    assert SiteConfig(name="A", url="https://a.com/path/").origin == "https://a.com"


def test_bad_url_rejected():
    with pytest.raises(ValueError):
        SiteConfig(name="A", url="https://")


def test_duplicate_names_rejected():
    with pytest.raises(ValueError, match="unique"):
        AppConfig.model_validate({
            "source": {"name": "Same", "url": "a.com"}, "competitors": [{"name": "same", "url": "b.com"}],
        })


def test_competitor_limits():
    with pytest.raises(ValueError):
        AppConfig.model_validate({"source": {"name": "S", "url": "s.com"}, "competitors": []})


def test_load_config_errors(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("source: {name: S}\ncompetitors: []\n")
    with pytest.raises(ConfigError, match="Invalid config"):
        load_config(bad)


def test_shipped_config_is_valid():
    cfg = load_config("config.yaml")
    assert cfg.currency == "INR" and len(cfg.competitors) >= 1
