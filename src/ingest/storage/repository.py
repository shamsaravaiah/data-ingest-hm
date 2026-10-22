"""Postgres upserts for sites, category trees, and products."""

import json
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ingest.crawl.extract import Crumb, ExtractedProduct
from ingest.db.models import Category, CrawlRun, Product, ProductImage, ProductVariant, Site, utcnow

logger = logging.getLogger(__name__)

GENERIC_NAMES = {
    "shop",
    "shop now",
    "view all",
    "see all",
    "discover",
    "explore",
    "new",
    "sale",
    "home",
    "menu",
    "category",
}


def record_error(run: CrawlRun, url: str, message: str) -> None:
    run.error_count += 1
    errors = list(run.recent_errors or [])
    errors.append({"url": url, "message": message[:300]})
    run.recent_errors = errors[-30:]
    run.error_message = message[:500]


def get_or_create_site(session: Session, base_url: str, host: str, prefix: str) -> Site:
    site = session.scalar(select(Site).where(Site.host == host, Site.locale_prefix == prefix))
    if site is None:
        site = Site(base_url=base_url, host=host, locale_prefix=prefix)
        session.add(site)
        session.flush()
    else:
        site.base_url = base_url
    return site


def create_run(session: Session, site: Site, start_url: str, max_pages: int) -> CrawlRun:
    run = CrawlRun(
        site_id=site.id,
        status="running",
        start_url=start_url,
        max_pages=max_pages,
        started_at=utcnow(),
        recent_errors=[],
    )
    session.add(run)
    session.flush()
    return run


def mark_interrupted(session: Session) -> None:
    runs = session.scalars(select(CrawlRun).where(CrawlRun.status.in_(("pending", "running")))).all()
    now = utcnow()
    for run in runs:
        run.status = "failed"
        run.error_message = run.error_message or "Crawl interrupted by process restart"
        run.finished_at = now


def _would_cycle(session: Session, category_id: int, parent_id: int | None) -> bool:
    seen = {category_id}
    current = parent_id
    while current:
        if current in seen:
            return True
        seen.add(current)
        parent = session.get(Category, current)
        if parent is None:
            return False
        current = parent.parent_id
    return False


def _path_for(session: Session, name: str, parent_id: int | None) -> tuple[str, int]:
    if parent_id is None:
        return name, 0
    parent = session.get(Category, parent_id)
    if parent is None or not parent.path:
        return name, 0 if parent is None else parent.depth + 1
    return f"{parent.path} / {name}", parent.depth + 1


def _replace_path(session: Session, category: Category, new_path: str, new_depth: int) -> None:
    old_path = category.path
    category.path = new_path
    category.depth = new_depth
    if not old_path or old_path == new_path:
        return
    descendants = session.scalars(
        select(Category).where(
            Category.site_id == category.site_id,
            Category.id != category.id,
        )
    ).all()
    for descendant in descendants:
        if descendant.path == old_path or descendant.path.startswith(old_path + " / "):
            descendant.path = new_path + descendant.path[len(old_path) :]
            descendant.depth = descendant.path.count(" / ")
    products = session.scalars(select(Product).where(Product.site_id == category.site_id)).all()
    for product in products:
        current = product.category_path or ""
        if current == old_path or current.startswith(old_path + " / "):
            product.category_path = new_path + current[len(old_path) :]


def _rename(session: Session, category: Category, new_name: str) -> None:
    category.name = new_name
    path, depth = _path_for(session, new_name, category.parent_id)
    _replace_path(session, category, path, depth)


def upsert_category(
    session: Session,
    site_id: int,
    url: str,
    name: str,
    parent_id: int | None = None,
    sort_order: int = 0,
    allow_reparent: bool = False,
) -> Category:
    cleaned = (name or "").strip()[:500] or "Category"
    row = session.scalar(select(Category).where(Category.site_id == site_id, Category.url == url))
    if parent_id is not None and row is not None and _would_cycle(session, row.id, parent_id):
        parent_id = row.parent_id
    if row is None:
        path, depth = _path_for(session, cleaned, parent_id)
        row = Category(
            site_id=site_id,
            parent_id=parent_id,
            name=cleaned,
            url=url,
            path=path,
            sort_order=sort_order,
            depth=depth,
        )
        session.add(row)
        session.flush()
        return row

    if (
        allow_reparent
        and parent_id
        and row.parent_id is None
        and parent_id != row.id
        and not _would_cycle(session, row.id, parent_id)
    ):
        row.parent_id = parent_id
        path, depth = _path_for(session, row.name, row.parent_id)
        _replace_path(session, row, path, depth)

    if row.name.lower() in GENERIC_NAMES and cleaned.lower() not in GENERIC_NAMES:
        _rename(session, row, cleaned)
    elif not row.path:
        path, depth = _path_for(session, row.name, row.parent_id)
        row.path = path
        row.depth = depth

    if sort_order and row.sort_order == 0:
        row.sort_order = sort_order
    session.flush()
    return row


def ensure_chain(session: Session, site_id: int, crumbs: list[Crumb]) -> list[Category]:
    parent_id: int | None = None
    created: list[Category] = []
    for index, crumb in enumerate(crumbs):
        if not crumb.url or not crumb.name:
            continue
        category = upsert_category(
            session,
            site_id,
            crumb.url,
            crumb.name,
            parent_id=parent_id,
            sort_order=index,
            allow_reparent=True,
        )
        created.append(category)
        parent_id = category.id
    return created


def _snapshot(product: ExtractedProduct) -> dict:
    raw = {
        "source": product.raw.get("source"),
        "name": product.name,
        "sku": product.sku,
        "description": product.description,
        "brand": product.brand,
        "price": product.price,
        "compare_at_price": product.compare_at_price,
        "currency": product.currency,
        "availability": product.availability,
        "images": [image.url for image in product.images],
        "variants": [
            {
                "sku": variant.sku,
                "color": variant.color,
                "size": variant.size,
                "price": variant.price,
                "currency": variant.currency,
                "availability": variant.availability,
            }
            for variant in product.variants
        ],
    }

    def convert(value):
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, dict):
            return {str(key): convert(item) for key, item in value.items()}
        if isinstance(value, list):
            return [convert(item) for item in value]
        return value

    safe = convert(raw)
    if len(json.dumps(safe)) > 100_000:
        safe.pop("description", None)
        safe.pop("variants", None)
    return safe


def _fill(current, new):
    if new is None or new == "":
        return current
    return new


def upsert_product(
    session: Session,
    site_id: int,
    extracted: ExtractedProduct,
    category_id: int | None,
    authoritative_category: bool,
    upload_image,
    max_images: int,
) -> Product | None:
    if not extracted.url and not extracted.sku:
        return None
    product_url = extracted.url or f"urn:sku:{extracted.sku}"
    row = session.scalar(select(Product).where(Product.site_id == site_id, Product.product_url == product_url))
    if row is None and extracted.sku:
        by_sku = session.scalar(select(Product).where(Product.site_id == site_id, Product.sku == extracted.sku))
        if by_sku is not None and (
            by_sku.product_url == product_url or by_sku.product_url.startswith("urn:sku:")
        ):
            row = by_sku
    if row is None:
        row = Product(site_id=site_id, product_url=product_url)
        session.add(row)
    elif extracted.url and row.product_url.startswith("urn:sku:"):
        taken = session.scalar(select(Product).where(Product.site_id == site_id, Product.product_url == extracted.url))
        if taken is None:
            row.product_url = extracted.url

    row.name = _fill(row.name, extracted.name)
    row.description = _fill(row.description, extracted.description)
    row.brand = _fill(row.brand, extracted.brand)
    row.sku = _fill(row.sku, extracted.sku)
    if extracted.price is not None:
        row.price = extracted.price
    if extracted.compare_at_price is not None:
        row.compare_at_price = extracted.compare_at_price
    row.currency = _fill(row.currency, extracted.currency)
    row.availability = _fill(row.availability, extracted.availability)
    if category_id and (authoritative_category or row.category_id is None):
        row.category_id = category_id
        category = session.get(Category, category_id)
        if category is not None:
            row.category_path = category.path
    row.raw_json = _snapshot(extracted)
    row.updated_at = utcnow()
    session.flush()

    if extracted.variants:
        row.variants.clear()
        session.flush()
        for variant in extracted.variants[:40]:
            row.variants.append(
                ProductVariant(
                    sku=variant.sku,
                    color=variant.color,
                    size=variant.size,
                    price=variant.price,
                    currency=variant.currency,
                    availability=variant.availability,
                )
            )

    if extracted.images:
        existing = {image.source_url: image.blob_url for image in list(row.images)}
        row.images.clear()
        session.flush()
        for position, image in enumerate(extracted.images[:max_images]):
            blob_url = existing.get(image.url)
            content_type = None
            if upload_image is not None and not blob_url:
                try:
                    blob_url, content_type = upload_image(image.url, row.sku, position)
                except Exception:
                    logger.warning("Image upload failed for %s", image.url, exc_info=True)
            row.images.append(
                ProductImage(
                    source_url=image.url,
                    blob_url=blob_url,
                    position=position,
                    content_type=content_type,
                    alt_text=image.alt,
                )
            )
    session.flush()
    return row
