"""Configuration loading and validation (config.yaml + .env)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

DEFAULT_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
]


class ConfigError(Exception):
    """Raised when config.yaml is missing or invalid."""


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


class SiteConfig(BaseModel):
    name: str = Field(min_length=1)
    url: str
    facebook_page_id: str | None = None
    instagram_handle: str | None = None
    youtube_channel: str | None = None

    @field_validator("url")
    @classmethod
    def _normalise_url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            v = "https://" + v
        parsed = urlparse(v)
        if not parsed.netloc:
            raise ValueError(f"not a valid URL: {v!r}")
        return v.rstrip("/")

    @property
    def origin(self) -> str:
        p = urlparse(self.url)
        return f"{p.scheme}://{p.netloc}"

    @property
    def key(self) -> str:
        return slugify(self.name)


class ScrapingSettings(BaseModel):
    rate_limit_per_sec: float = Field(default=1.0, gt=0, le=50)
    timeout_seconds: float = Field(default=20.0, gt=0)
    max_retries: int = Field(default=3, ge=1, le=10)
    respect_robots: bool = True
    use_browser: Literal["auto", "on", "off"] = "auto"  # headless-browser fallback for JavaScript-rendered sites
    cache_dir: str = "data/cache"
    cache_ttl_minutes: int = Field(default=60, ge=0)
    user_agents: list[str] = Field(default_factory=lambda: list(DEFAULT_USER_AGENTS))


class LLMConfig(BaseModel):
    provider: Literal["gemini", "groq", "openrouter", "ollama", "claude"] = "gemini"
    model: str = "gemini-2.5-flash"
    api_key_env: str = "GEMINI_API_KEY"


class AlertsConfig(BaseModel):
    email: str | None = None
    telegram: str | None = None
    price_drop_threshold_pct: float = 10.0


class AppConfig(BaseModel):
    source: SiteConfig
    competitors: list[SiteConfig] = Field(min_length=1, max_length=6)
    categories_to_track: list[str] = Field(default_factory=list)
    max_products_per_site: int = Field(default=200, ge=1)
    country: str = "IN"
    currency: str = "INR"
    schedule: str = "daily"
    match_threshold: float = Field(default=0.85, ge=0, le=1)
    db_url: str = "sqlite:///data/competitor_intel.db"
    scraping: ScrapingSettings = Field(default_factory=ScrapingSettings)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)

    @model_validator(mode="after")
    def _unique_sites(self) -> AppConfig:
        keys = [s.key for s in self.all_sites_flat()]
        dupes = {k for k in keys if keys.count(k) > 1}
        if dupes:
            raise ValueError(f"site names must be unique; duplicates: {sorted(dupes)}")
        return self

    def all_sites_flat(self) -> list[SiteConfig]:
        return [self.source, *self.competitors]

    def all_sites(self) -> list[tuple[SiteConfig, bool]]:
        """(site, is_source) pairs, source first."""
        return [(self.source, True), *[(c, False) for c in self.competitors]]


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    load_dotenv()
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Config file not found: {p}. Run `python -m src.cli init` first.")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse {p}: {exc}") from exc
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        lines = [f"  - {'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ConfigError(f"Invalid config in {p}:\n" + "\n".join(lines)) from exc
