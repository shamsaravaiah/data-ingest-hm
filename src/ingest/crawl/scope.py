import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

LOCALE_OR_INDEX = {"index.html", "index.htm", "index.php", "index.aspx", "home"}

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "gclid",
    "fbclid",
    "srsltid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "_ga",
    "ref",
    "affiliate",
}

SKIP_SEGMENTS = {
    "login",
    "log-in",
    "signin",
    "sign-in",
    "signup",
    "sign-up",
    "register",
    "account",
    "my-account",
    "myaccount",
    "cart",
    "basket",
    "bag",
    "checkout",
    "wishlist",
    "favourites",
    "favorites",
    "customer-service",
    "customerservice",
    "help",
    "support",
    "contact",
    "stores",
    "store-locator",
    "storelocator",
    "privacy",
    "cookie",
    "cookies",
    "terms",
    "legal",
    "sitemap",
    "search",
    "newsletter",
    "gift-card",
    "giftcard",
    "careers",
    "about",
    "faq",
}

SKIP_EXTENSIONS = {
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".css",
    ".js",
    ".zip",
    ".xml",
    ".ico",
    ".mp4",
    ".webm",
}


def normalize_base(url: str) -> tuple[str, str, str]:
    """Return normalized URL, host, and path prefix that later pages must stay under."""
    raw = url.strip()
    if not raw:
        raise ValueError("URL is required")
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if not parsed.netloc or " " in parsed.netloc:
        raise ValueError("URL must include a host")
    host = parsed.netloc.lower()
    raw_path = re.sub(r"/+", "/", parsed.path or "/")
    segments = [segment for segment in raw_path.split("/") if segment]
    if segments and segments[-1].lower() in LOCALE_OR_INDEX:
        segments = segments[:-1]
    prefix = "/" + "/".join(segments) if segments else ""
    normalized = urlunparse((parsed.scheme.lower(), host, prefix, "", "", ""))
    return normalized, host, prefix


def absolutize(url: str, base: str) -> str | None:
    """Resolve an image or asset URL without the link filters used for crawling."""
    if not url:
        return None
    stripped = url.strip()
    if not stripped or stripped.startswith(("data:", "javascript:", "mailto:")):
        return None
    absolute = urljoin(base, stripped)
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    path = re.sub(r"/+", "/", parsed.path or "/")
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", parsed.query, ""))


def canonicalize(url: str, base: str) -> str | None:
    if not url:
        return None
    absolute = urljoin(base, url.strip())
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    path = re.sub(r"/+", "/", parsed.path or "/")
    lower_path = path.lower()
    for extension in SKIP_EXTENSIONS:
        if lower_path.endswith(extension):
            return None
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    pairs: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=False):
        if key.lower() in TRACKING_PARAMS:
            continue
        pairs.append((key, value))
    if len(pairs) > 2:
        return None
    pairs.sort()
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", urlencode(pairs), ""))


def in_scope(url: str, host: str, prefix: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.netloc.lower() != host.lower():
        return False
    path = parsed.path.rstrip("/") or "/"
    if not prefix:
        return True
    prefix_norm = prefix.rstrip("/") or "/"
    return path == prefix_norm or path.startswith(prefix_norm + "/")


def should_skip_path(url: str) -> bool:
    segments = [segment for segment in urlparse(url).path.lower().split("/") if segment]
    for segment in segments:
        cleaned = segment.replace("_", "-")
        if cleaned in SKIP_SEGMENTS:
            return True
        if "." in cleaned:
            name = cleaned.rsplit(".", 1)[0]
            if name in SKIP_SEGMENTS:
                return True
    return False


def urls_match(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return left.rstrip("/") == right.rstrip("/")
