"""Playwright fetch of one public page, plus JSON the page itself requested."""

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from ingest.config import Settings

logger = logging.getLogger(__name__)

ANALYTICS_PARTS = (
    "google-analytics",
    "googletagmanager",
    "googleadservices",
    "doubleclick",
    "facebook.com",
    "facebook.net",
    "segment.io",
    "segment.com",
    "newrelic",
    "hotjar",
    "sentry.io",
    "clarity.ms",
    "demdex",
    "omtrdc",
    "onetrust",
    "cookielaw",
    "qualtrics",
    "optimizely",
    "contentsquare",
    "adservice",
    "pinimg",
    "pinterest",
    "tiktok",
    "snapchat",
)

BLOCK_TITLE_MARKERS = (
    "captcha",
    "access denied",
    "just a moment",
    "verify you are a human",
    "are you a robot",
    "bot verification",
)


@dataclass
class FetchResult:
    url: str
    status: int
    html: str
    json_payloads: list[Any] = field(default_factory=list)
    blocked: bool = False
    block_reason: str | None = None


def _is_analytics(url: str) -> bool:
    lower = url.lower()
    return any(part in lower for part in ANALYTICS_PARTS)


def _blocked_reason(status: int, html: str) -> str | None:
    if status in (401, 403, 429, 503):
        return f"http {status}"
    title = ""
    lower = html.lower()
    start = lower.find("<title")
    if start != -1:
        open_end = lower.find(">", start)
        close = lower.find("</title>", open_end)
        if open_end != -1 and close != -1:
            title = html[open_end + 1 : close].strip().lower()
    for marker in BLOCK_TITLE_MARKERS:
        if marker in title:
            return marker
    return None


def read_json_responses(responses: list[Any], limit: int = 40) -> list[Any]:
    payloads: list[Any] = []
    for response in responses[:limit]:
        try:
            url = response.url
            if _is_analytics(url):
                continue
            headers = response.headers
            content_type = headers.get("content-type", "")
            if "json" not in content_type.lower():
                continue
            if response.status >= 400:
                continue
            body = response.body()
        except Exception:
            continue
        if not body or len(body) > 1_500_000:
            continue
        try:
            payloads.append(json.loads(body.decode("utf-8", "replace")))
        except (UnicodeError, json.JSONDecodeError):
            continue
    return payloads


def fetch_page(page: Any, url: str, bucket: list[Any], settings: Settings) -> FetchResult:
    """Load a page. `bucket` collects response objects from the page listener."""
    bucket.clear()
    timeout_ms = int(settings.request_timeout_seconds * 1000)
    response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        logger.debug("networkidle timed out for %s", url)
    for _ in range(3):
        try:
            page.evaluate("window.scrollBy(0, Math.max(window.innerHeight, 800))")
            page.wait_for_timeout(400)
        except Exception:
            break
    status = response.status if response is not None else 0
    final_url = page.url
    html = page.content()
    responses = list(bucket)
    bucket.clear()
    payloads = read_json_responses(responses)
    reason = _blocked_reason(status, html)
    return FetchResult(
        url=final_url,
        status=status,
        html=html,
        json_payloads=payloads,
        blocked=reason is not None,
        block_reason=reason,
    )


def launch_browser(playwright: Any, settings: Settings):
    browser = playwright.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
    context = browser.new_context(
        user_agent=settings.user_agent,
        viewport={"width": 1366, "height": 900},
    )
    page = context.new_page()
    page.set_default_navigation_timeout(int(settings.request_timeout_seconds * 1000))

    def _route(route: Any) -> None:
        if route.request.resource_type in {"image", "media", "font"}:
            route.abort()
        else:
            route.continue_()

    page.route("**/*", _route)
    return browser, page
