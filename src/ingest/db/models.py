from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Site(Base):
    __tablename__ = "sites"
    __table_args__ = (UniqueConstraint("host", "locale_prefix", name="uq_sites_host_prefix"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    locale_prefix: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    categories: Mapped[list["Category"]] = relationship(back_populates="site")
    products: Mapped[list["Product"]] = relationship(back_populates="site")
    runs: Mapped[list["CrawlRun"]] = relationship(back_populates="site")


class CrawlRun(Base):
    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    start_url: Mapped[str] = mapped_column(Text, nullable=False)
    max_pages: Mapped[int] = mapped_column(Integer, nullable=False)
    pages_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    products_upserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    categories_upserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    recent_errors: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    site: Mapped[Site] = relationship(back_populates="runs")


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("site_id", "url", name="uq_categories_site_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), nullable=False, index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    site: Mapped[Site] = relationship(back_populates="categories")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("site_id", "product_url", name="uq_products_site_url"),
        Index("ix_products_site_sku", "site_id", "sku"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), nullable=False, index=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True, index=True)
    sku: Mapped[str | None] = mapped_column(String(128), nullable=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    brand: Mapped[str | None] = mapped_column(String(255), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    compare_at_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    availability: Mapped[str | None] = mapped_column(String(64), nullable=True)
    product_url: Mapped[str] = mapped_column(Text, nullable=False)
    category_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    site: Mapped[Site] = relationship(back_populates="products")
    category: Mapped[Category | None] = relationship()
    variants: Mapped[list["ProductVariant"]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
    )
    images: Mapped[list["ProductImage"]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        order_by="ProductImage.position",
    )


class ProductVariant(Base):
    __tablename__ = "product_variants"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sku: Mapped[str | None] = mapped_column(String(128), nullable=True)
    color: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size: Mapped[str | None] = mapped_column(String(128), nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    availability: Mapped[str | None] = mapped_column(String(64), nullable=True)

    product: Mapped[Product] = relationship(back_populates="variants")


class ProductImage(Base):
    __tablename__ = "product_images"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    blob_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    alt_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    product: Mapped[Product] = relationship(back_populates="images")
