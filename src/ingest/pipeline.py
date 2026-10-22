"""Crawl one base URL and write the catalog."""

import hashlib
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass

import httpx

from ingest.config import Settings, get_settings
from ingest.crawl.extract import extract_page, is_pagination, label_from_url
from ingest.crawl.fetch import _is_analytics, fetch_page, launch_browser
from ingest.crawl.robots import Robots, origin_of
from ingest.crawl.scope import canonicalize, in_scope, normalize_base, should_skip_path, urls_match
from ingest.db.models import CrawlRun, Site, utcnow
from ingest.db.session import get_session_factory, init_db
from ingest.storage.blob import BlobStore, blob_name, download_image
from ingest.storage.repository import (
    create_run,
    ensure_chain,
    get_or_create_site,
    record_error,
    upsert_category,
    upsert_product,
)

logger = logging.getLogger(__name__)


@dataclass
class Task:
    url: str
    parent_category_id: int | None
    depth: int
    kind: str
    name: str | None = None
    sort_order: int = 0


def clamp_max_pages(value: int) -> int:
    if value < 1 or value > 200_000:
        raise ValueError("max_pages must be between 1 and 200000")
    return value


def start_crawl(url: str, max_pages: int | None = None, settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    init_db(settings)
    normalized, host, prefix = normalize_base(url)
    pages = clamp_max_pages(max_pages if max_pages is not None else settings.max_pages)
    session = get_session_factory(settings)()
    try:
        site = get_or_create_site(session, normalized, host, prefix)
        run = create_run(session, site, normalized, pages)
        session.commit()
        return run.id
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _page_dead(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(part in text for part in ("closed", "crash", "target page", "browser has been closed"))


def _listen(page, bucket: list) -> None:
    def on_response(response) -> None:
        if len(bucket) >= 40:
            return
        try:
            content_type = response.headers.get("content-type", "")
            url = response.url
        except Exception:
            return
        if "json" not in content_type.lower() or _is_analytics(url):
            return
        bucket.append(response)

    page.on("response", on_response)


def _image_uploader(client: httpx.Client, blob: BlobStore, settings: Settings, host: str):
    if not blob.enabled:
        return None

    def upload(url: str, sku: str | None, position: int):
        downloaded = download_image(client, url, settings.max_image_bytes, settings.user_agent)
        if downloaded is None:
            return None, None
        data, content_type = downloaded
        digest = hashlib.sha256(url.encode()).hexdigest()[:16]
        name = blob_name(host, sku, digest, position, content_type)
        return blob.upload(data, content_type, name), content_type

    return upload


def _scoped_crumbs(crumbs, host: str, prefix: str):
    return [crumb for crumb in crumbs if crumb.url and crumb.name and in_scope(crumb.url, host, prefix)]


def _mark_stopped(run, session) -> None:
    if run.status != "running":
        return
    run.status = "stopped"
    run.error_message = "Stopped"
    run.finished_at = utcnow()
    session.commit()


def execute_crawl(
    run_id: int,
    settings: Settings | None = None,
    cancel: threading.Event | None = None,
) -> None:
    settings = settings or get_settings()
    init_db(settings)
    session = get_session_factory(settings)()
    http = httpx.Client(headers={"User-Agent": settings.user_agent}, follow_redirects=True, timeout=30)
    browser = None
    try:
        run = session.get(CrawlRun, run_id)
        if run is None:
            raise ValueError(f"Crawl run {run_id} was not found")
        site = session.get(Site, run.site_id)
        if site is None:
            raise ValueError(f"Site for crawl run {run_id} was not found")
        run.status = "running"
        if run.started_at is None:
            run.started_at = utcnow()
        session.commit()

        blob = BlobStore(settings.azure_storage_connection_string, settings.azure_blob_container)
        if blob.enabled:
            logger.info("Image files will be uploaded to Azure Blob container %s", settings.azure_blob_container)
        else:
            logger.info("Azure Blob is not configured; storing image URLs only")

        robots = Robots.load(origin_of(run.start_url), settings.user_agent, http)
        delay = max(0.0, settings.crawl_delay_seconds)
        if robots.crawl_delay:
            delay = max(delay, robots.crawl_delay)
        if not robots.allowed(run.start_url):
            run.status = "failed"
            run.error_message = "robots.txt disallows the start URL"
            run.finished_at = utcnow()
            session.commit()
            return

        queue: deque[Task] = deque()
        visited: set[str] = set()
        scheduled: set[str] = set()
        pagination_counts: dict[str, int] = {}
        product_ids: set[int] = set()
        category_ids: set[int] = set()
        queue_limit = max(run.max_pages * 4, run.max_pages)
        uploader = _image_uploader(http, blob, settings, site.host)

        def enqueue(task: Task) -> None:
            canon = canonicalize(task.url, task.url)
            if canon is None:
                return
            task.url = canon
            if canon in visited or canon in scheduled:
                return
            if len(scheduled) >= queue_limit:
                return
            if not in_scope(canon, site.host, site.locale_prefix) or should_skip_path(canon):
                return
            scheduled.add(canon)
            queue.append(task)

        enqueue(Task(url=run.start_url, parent_category_id=None, depth=0, kind="category"))

        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser, page = launch_browser(playwright, settings)
            bucket: list = []
            _listen(page, bucket)

            while queue and run.pages_fetched < run.max_pages:
                if cancel is not None and cancel.is_set():
                    _mark_stopped(run, session)
                    return
                task = queue.popleft()
                scheduled.discard(task.url)
                if task.url in visited:
                    continue
                if not robots.allowed(task.url):
                    logger.info("Skipping %s because robots.txt disallows it", task.url)
                    visited.add(task.url)
                    continue
                visited.add(task.url)

                result = None
                error: Exception | None = None
                try:
                    result = fetch_page(page, task.url, bucket, settings)
                except Exception as exc:
                    error = exc
                    if _page_dead(exc):
                        logger.warning("Browser failed on %s; relaunching", task.url)
                        try:
                            browser.close()
                        except Exception:
                            pass
                        browser, page = launch_browser(playwright, settings)
                        bucket = []
                        _listen(page, bucket)
                        try:
                            result = fetch_page(page, task.url, bucket, settings)
                            error = None
                        except Exception as retry_exc:
                            error = retry_exc
                if result is None:
                    record_error(run, task.url, str(error or "fetch failed"))
                    session.commit()
                    time.sleep(delay)
                    continue

                if not in_scope(result.url, site.host, site.locale_prefix):
                    record_error(run, result.url, "Redirected outside the base URL scope")
                    session.commit()
                    time.sleep(delay)
                    continue

                run.pages_fetched += 1
                if result.blocked:
                    record_error(run, result.url, f"Blocked: {result.block_reason}")
                    if task.depth == 0 and urls_match(task.url, run.start_url):
                        run.status = "failed"
                        run.error_message = (
                            f"Start URL blocked ({result.block_reason}). "
                            "The crawler does not bypass bot checks."
                        )
                        run.finished_at = utcnow()
                        session.commit()
                        return
                    session.commit()
                    time.sleep(delay)
                    continue

                is_home = task.depth == 0 and not is_pagination(task.url, "")
                extracted = extract_page(
                    result.html,
                    result.url,
                    result.json_payloads,
                    host=site.host,
                    prefix=site.locale_prefix,
                    is_home=is_home,
                )
                current_category_id = _persist_page(
                    session,
                    site.id,
                    site.host,
                    site.locale_prefix,
                    task,
                    extracted,
                    result.url,
                    is_home,
                    category_ids,
                    product_ids,
                    uploader,
                    settings.max_images_per_product,
                )
                _enqueue_discovered(
                    extracted,
                    task,
                    current_category_id,
                    site.id,
                    site.host,
                    site.locale_prefix,
                    settings.max_depth,
                    session,
                    category_ids,
                    pagination_counts,
                    enqueue,
                )
                run.categories_upserted = len(category_ids)
                run.products_upserted = len(product_ids)
                session.commit()
                logger.info(
                    "fetched %s/%s %s products_on_page=%s saved_products=%s queue=%s",
                    run.pages_fetched,
                    run.max_pages,
                    result.url,
                    len(extracted.products),
                    run.products_upserted,
                    len(queue),
                )
                if cancel is not None and cancel.wait(delay):
                    _mark_stopped(run, session)
                    return
                if cancel is None and delay:
                    time.sleep(delay)

        if run.status == "running":
            run.status = "completed"
            run.finished_at = utcnow()
            session.commit()
            logger.info(
                "Crawl %s completed: %s pages, %s products, %s categories",
                run.id,
                run.pages_fetched,
                run.products_upserted,
                run.categories_upserted,
            )
    except Exception as exc:
        stopped = cancel is not None and cancel.is_set()
        logger.exception("Crawl %s failed", run_id)
        try:
            session.rollback()
        except Exception:
            pass
        try:
            failed = session.get(CrawlRun, run_id)
            if failed is not None and failed.status not in ("completed", "stopped"):
                failed.status = "stopped" if stopped else "failed"
                failed.error_message = "Stopped" if stopped else str(exc)[:500]
                failed.finished_at = utcnow()
                session.commit()
        except Exception:
            logger.exception("Could not record the crawl failure")
        if not stopped:
            raise
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        http.close()
        session.close()


def _persist_page(
    session,
    site_id: int,
    host: str,
    prefix: str,
    task: Task,
    extracted,
    final_url: str,
    is_home: bool,
    category_ids: set[int],
    product_ids: set[int],
    uploader,
    max_images: int,
) -> int | None:
    crumbs = _scoped_crumbs(extracted.breadcrumbs, host, prefix)
    current_category_id = task.parent_category_id
    paginated = is_pagination(final_url, "") or is_pagination(task.url, "")

    if extracted.is_product:
        items = list(crumbs)
        main = next((product for product in extracted.products if product.is_main), None)
        if items:
            last = items[-1]
            if main and main.name and last.name.lower() == main.name.lower():
                items = items[:-1]
            elif last.url and urls_match(last.url, final_url):
                items = items[:-1]
        chain = ensure_chain(session, site_id, items)
        category_ids.update(category.id for category in chain)
        if chain:
            current_category_id = chain[-1].id
    elif paginated:
        current_category_id = task.parent_category_id
    elif not is_home:
        name = extracted.heading or task.name or label_from_url(final_url) or "Category"
        parent_id = task.parent_category_id
        page_crumbs = list(crumbs)
        if page_crumbs and page_crumbs[-1].url and urls_match(page_crumbs[-1].url, final_url):
            page_crumbs = page_crumbs[:-1]
        chain = ensure_chain(session, site_id, page_crumbs)
        category_ids.update(category.id for category in chain)
        if chain:
            parent_id = chain[-1].id
        category = upsert_category(
            session,
            site_id,
            final_url,
            name,
            parent_id=parent_id,
            sort_order=task.sort_order,
            allow_reparent=True,
        )
        category_ids.add(category.id)
        current_category_id = category.id
    else:
        # A locale home such as /en_us keeps the header (Women, Men, Kids) as roots.
        # A deeper base URL such as /en_us/men becomes the root of that section.
        section = label_from_url(final_url)
        if section:
            heading = extracted.heading
            name = heading if heading and len(heading) <= 60 else (task.name or section)
            category = upsert_category(
                session,
                site_id,
                final_url,
                name,
                parent_id=None,
                sort_order=0,
                allow_reparent=False,
            )
            category_ids.add(category.id)
            current_category_id = category.id

    for product in extracted.products:
        authoritative = (not extracted.is_product) or product.is_main
        if product.url and (not in_scope(product.url, host, prefix) or should_skip_path(product.url)):
            continue
        saved = upsert_product(
            session,
            site_id,
            product,
            current_category_id if authoritative else None,
            authoritative,
            uploader,
            max_images,
        )
        if saved is not None:
            product_ids.add(saved.id)
    return current_category_id


def _enqueue_discovered(
    extracted,
    task: Task,
    current_category_id: int | None,
    site_id: int,
    host: str,
    prefix: str,
    max_depth: int,
    session,
    category_ids: set[int],
    pagination_counts: dict[str, int],
    enqueue,
) -> None:
    for link in extracted.links:
        if not in_scope(link.url, host, prefix) or should_skip_path(link.url):
            continue
        if link.kind == "pagination":
            path = link.url.split("?", 1)[0]
            followed = pagination_counts.get(path, 0)
            if followed >= 20:
                continue
            pagination_counts[path] = followed + 1
            enqueue(
                Task(
                    url=link.url,
                    parent_category_id=current_category_id,
                    depth=task.depth,
                    kind="category",
                    name=task.name,
                    sort_order=task.sort_order,
                )
            )
            continue
        if link.kind == "category":
            if task.depth >= max_depth:
                continue
            category = upsert_category(
                session,
                site_id,
                link.url,
                link.name,
                parent_id=current_category_id,
                sort_order=link.sort_order,
                allow_reparent=False,
            )
            category_ids.add(category.id)
            enqueue(
                Task(
                    url=link.url,
                    parent_category_id=current_category_id,
                    depth=task.depth + 1,
                    kind="category",
                    name=link.name,
                    sort_order=link.sort_order,
                )
            )
            continue
        if task.depth > max_depth:
            continue
        enqueue(
            Task(
                url=link.url,
                parent_category_id=current_category_id,
                depth=task.depth + 1,
                kind="product",
                name=link.name,
                sort_order=link.sort_order,
            )
        )
