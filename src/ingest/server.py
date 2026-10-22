"""HTTP API for starting a crawl and reading the catalog."""

import logging
import threading
from contextlib import asynccontextmanager
from decimal import Decimal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from ingest.config import get_settings
from ingest.db.models import Category, CrawlRun, Product, Site
from ingest.db.session import init_db, session_scope
from ingest.pipeline import execute_crawl, start_crawl
from ingest.storage.repository import mark_interrupted

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_state = {"running": False}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    init_db()
    with session_scope() as session:
        mark_interrupted(session)
    yield


app = FastAPI(title="Catalog ingest", version="1.0.0", lifespan=lifespan)


class CrawlIn(BaseModel):
    url: str
    max_pages: int | None = Field(default=None, ge=1, le=200_000)


def _money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return f"{Decimal(value):.2f}"


def _iso(value) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _run_payload(run: CrawlRun, host: str) -> dict:
    return {
        "id": run.id,
        "status": run.status,
        "host": host,
        "start_url": run.start_url,
        "max_pages": run.max_pages,
        "pages_fetched": run.pages_fetched,
        "products_upserted": run.products_upserted,
        "categories_upserted": run.categories_upserted,
        "error_count": run.error_count,
        "error_message": run.error_message,
        "recent_errors": run.recent_errors or [],
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
    }


def _pick_site(session, host: str, prefix: str | None) -> Site:
    sites = list(session.scalars(select(Site).where(Site.host == host.lower())).all())
    if prefix is not None:
        wanted = prefix if prefix.startswith("/") or prefix == "" else f"/{prefix}"
        if wanted != "/":
            wanted = wanted.rstrip("/")
        else:
            wanted = ""
        sites = [site for site in sites if site.locale_prefix == wanted]
    if not sites:
        raise HTTPException(status_code=404, detail="Site not found")
    if len(sites) == 1:
        return sites[0]
    latest = None
    chosen = sites[0]
    for site in sites:
        run = session.scalar(
            select(CrawlRun).where(CrawlRun.site_id == site.id).order_by(CrawlRun.id.desc())
        )
        if run is not None and (latest is None or run.id > latest):
            latest = run.id
            chosen = site
    return chosen


def _tree(categories: list[Category]) -> list[dict]:
    nodes = {
        category.id: {
            "id": category.id,
            "parent_id": category.parent_id,
            "name": category.name,
            "url": category.url,
            "path": category.path,
            "sort_order": category.sort_order,
            "depth": category.depth,
            "children": [],
        }
        for category in categories
    }
    roots: list[dict] = []
    for category in categories:
        node = nodes[category.id]
        parent = nodes.get(category.parent_id) if category.parent_id else None
        if parent is not None:
            parent["children"].append(node)
        else:
            roots.append(node)

    def sort_nodes(items: list[dict]) -> None:
        items.sort(key=lambda item: (item["sort_order"], item["name"].lower()))
        for item in items:
            sort_nodes(item["children"])

    sort_nodes(roots)
    return roots


def _descendant_ids(categories: list[Category], root_id: int) -> list[int]:
    children: dict[int | None, list[int]] = {}
    for category in categories:
        children.setdefault(category.parent_id, []).append(category.id)
    ids: list[int] = []
    stack = [root_id]
    while stack:
        current = stack.pop()
        ids.append(current)
        stack.extend(children.get(current, []))
    return ids


def _product_payload(product: Product) -> dict:
    images = sorted(product.images, key=lambda image: image.position)
    return {
        "id": product.id,
        "sku": product.sku,
        "name": product.name,
        "description": product.description,
        "brand": product.brand,
        "currency": product.currency,
        "price": _money(product.price),
        "compare_at_price": _money(product.compare_at_price),
        "availability": product.availability,
        "product_url": product.product_url,
        "category_id": product.category_id,
        "category_path": product.category_path,
        "images": [
            {
                "source_url": image.source_url,
                "blob_url": image.blob_url,
                "position": image.position,
                "alt": image.alt_text,
            }
            for image in images
        ],
        "variants": [
            {
                "sku": variant.sku,
                "color": variant.color,
                "size": variant.size,
                "price": _money(variant.price),
                "currency": variant.currency,
                "availability": variant.availability,
            }
            for variant in product.variants
        ],
    }


def _worker(run_id: int) -> None:
    try:
        execute_crawl(run_id, get_settings())
    except Exception:
        logger.exception("Background crawl %s failed", run_id)
    finally:
        with _lock:
            _state["running"] = False


@app.get("/health")
def health() -> dict:
    with session_scope() as session:
        session.execute(text("select 1"))
    return {"status": "ok"}


@app.post("/v1/crawls", status_code=202)
def create_crawl(body: CrawlIn) -> dict:
    with _lock:
        if _state["running"]:
            raise HTTPException(status_code=409, detail="A crawl is already running")
        _state["running"] = True
    try:
        run_id = start_crawl(body.url, body.max_pages, get_settings())
    except ValueError as exc:
        with _lock:
            _state["running"] = False
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        with _lock:
            _state["running"] = False
        raise
    threading.Thread(target=_worker, args=(run_id,), daemon=True).start()
    return {"id": run_id, "status": "running"}


@app.get("/v1/crawls/{run_id}")
def get_crawl(run_id: int) -> dict:
    with session_scope() as session:
        run = session.get(CrawlRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Crawl not found")
        site = session.get(Site, run.site_id)
        return _run_payload(run, site.host if site else "")


@app.get("/v1/sites/{host}/categories")
def list_categories(host: str, prefix: str | None = None) -> dict:
    with session_scope() as session:
        site = _pick_site(session, host, prefix)
        categories = list(
            session.scalars(select(Category).where(Category.site_id == site.id)).all()
        )
        return {
            "host": site.host,
            "locale_prefix": site.locale_prefix,
            "categories": _tree(categories),
        }


@app.get("/v1/sites/{host}/products")
def list_products(
    host: str,
    prefix: str | None = None,
    category_id: int | None = None,
    category: str | None = Query(default=None, description="Category path, for example Women / Dresses"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    with session_scope() as session:
        site = _pick_site(session, host, prefix)
        categories = list(session.scalars(select(Category).where(Category.site_id == site.id)).all())
        stmt = (
            select(Product)
            .where(Product.site_id == site.id)
            .options(selectinload(Product.images), selectinload(Product.variants))
            .order_by(Product.id)
        )
        if category_id is not None:
            if not any(item.id == category_id and item.site_id == site.id for item in categories):
                raise HTTPException(status_code=404, detail="Category not found")
            stmt = stmt.where(Product.category_id.in_(_descendant_ids(categories, category_id)))
        elif category:
            match = next((item for item in categories if item.path == category), None)
            if match is None:
                raise HTTPException(status_code=404, detail="Category not found")
            stmt = stmt.where(Product.category_id.in_(_descendant_ids(categories, match.id)))
        rows = list(session.scalars(stmt.offset(offset).limit(limit)).all())
        return {
            "host": site.host,
            "locale_prefix": site.locale_prefix,
            "limit": limit,
            "offset": offset,
            "products": [_product_payload(product) for product in rows],
        }
