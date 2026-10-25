"""SQLAlchemy models. Snapshots are append-only: history is never overwritten."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|success|partial|failed
    notes: Mapped[str | None] = mapped_column(Text)


class Site(Base):
    __tablename__ = "sites"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(500))
    is_source: Mapped[bool] = mapped_column(Boolean, default=False)
    platform: Mapped[str | None] = mapped_column(String(30))
    facebook_page_id: Mapped[str | None] = mapped_column(String(100))
    instagram_handle: Mapped[str | None] = mapped_column(String(100))
    youtube_channel: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    products: Mapped[list[Product]] = relationship(back_populates="site")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("site_id", "external_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(300))
    url: Mapped[str | None] = mapped_column(String(1000))
    title: Mapped[str] = mapped_column(String(500))
    brand: Mapped[str | None] = mapped_column(String(200))
    sku: Mapped[str | None] = mapped_column(String(200), index=True)
    gtin: Mapped[str | None] = mapped_column(String(50), index=True)
    mpn: Mapped[str | None] = mapped_column(String(200))
    category: Mapped[str | None] = mapped_column(String(300))
    image_url: Mapped[str | None] = mapped_column(String(1000))
    tags: Mapped[list | None] = mapped_column(JSON)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    site: Mapped[Site] = relationship(back_populates="products")


class PriceSnapshot(Base):
    __tablename__ = "price_snapshots"
    __table_args__ = (UniqueConstraint("product_id", "run_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    price: Mapped[float | None] = mapped_column(Float)
    compare_at_price: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(10))
    in_stock: Mapped[bool | None] = mapped_column(Boolean)  # None = unknown
    low_stock_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    is_preorder: Mapped[bool] = mapped_column(Boolean, default=False)
    rating: Mapped[float | None] = mapped_column(Float)
    review_count: Mapped[int | None] = mapped_column(Integer)
    scraped_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class VariantSnapshot(Base):
    """Per-variant price/stock (size, colour...). Added so stock is tracked per variant."""
    __tablename__ = "variant_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    variant_external_id: Mapped[str | None] = mapped_column(String(100))
    title: Mapped[str | None] = mapped_column(String(300))
    sku: Mapped[str | None] = mapped_column(String(200))
    price: Mapped[float | None] = mapped_column(Float)
    compare_at_price: Mapped[float | None] = mapped_column(Float)
    available: Mapped[bool | None] = mapped_column(Boolean)


class ProductMatch(Base):
    __tablename__ = "product_matches"
    __table_args__ = (UniqueConstraint("source_product_id", "competitor_product_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    competitor_product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    confidence: Mapped[float] = mapped_column(Float)
    method: Mapped[str] = mapped_column(String(30))  # sku|gtin|brand_model|fuzzy|embedding|manual
    status: Mapped[str] = mapped_column(String(20), default="auto")  # auto|pending|confirmed|rejected
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)


class Offer(Base):
    __tablename__ = "offers"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    type: Mapped[str] = mapped_column(String(40))  # banner|coupon|free_shipping|bundle|bank_offer|emi...
    text: Mapped[str | None] = mapped_column(Text)
    code: Mapped[str | None] = mapped_column(String(100))
    discount_value: Mapped[float | None] = mapped_column(Float)
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Ad(Base):
    __tablename__ = "ads"
    __table_args__ = (UniqueConstraint("site_id", "platform", "ad_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    platform: Mapped[str] = mapped_column(String(20))  # meta|google
    ad_id: Mapped[str] = mapped_column(String(200))
    copy: Mapped[str | None] = mapped_column(Text)
    creative_url: Mapped[str | None] = mapped_column(String(1000))
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class SocialSnapshot(Base):
    __tablename__ = "social_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    platform: Mapped[str] = mapped_column(String(20))
    followers: Mapped[int | None] = mapped_column(Integer)
    posts_last_30d: Mapped[int | None] = mapped_column(Integer)
    engagement_rate: Mapped[float | None] = mapped_column(Float)
    scraped_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ScrapeLog(Base):
    """Per-site outcome of each run; feeds the report's data-quality appendix."""
    __tablename__ = "scrape_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    platform_detected: Mapped[str | None] = mapped_column(String(30))
    method: Mapped[str | None] = mapped_column(String(50))
    product_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20))  # ok|failed
    error: Mapped[str | None] = mapped_column(Text)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Analysis(Base):
    """A website-submitted analysis job (one source URL + competitor URLs) and its result."""
    __tablename__ = "analyses"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|running|done|failed
    source_url: Mapped[str] = mapped_column(String(500))
    competitor_urls: Mapped[list] = mapped_column(JSON)
    options: Mapped[dict | None] = mapped_column(JSON)
    progress: Mapped[dict | None] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[int | None] = mapped_column(Integer)
    monitor_id: Mapped[str | None] = mapped_column(String(32), index=True)


class Monitor(Base):
    """A saved comparison that re-runs on a schedule and can send alerts (email / Telegram)."""
    __tablename__ = "monitors"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    source_url: Mapped[str] = mapped_column(String(500))
    competitor_urls: Mapped[list] = mapped_column(JSON)
    options: Mapped[dict | None] = mapped_column(JSON)
    interval_hours: Mapped[int] = mapped_column(Integer, default=24)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_email: Mapped[str | None] = mapped_column(String(200))
    telegram_chat_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_analysis_id: Mapped[str | None] = mapped_column(String(32))
