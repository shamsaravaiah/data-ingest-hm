"""Turn a rendered page into categories, products, and follow-up links."""

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, unquote, urlparse

from bs4 import BeautifulSoup, Tag

from ingest.crawl.scope import absolutize, canonicalize, in_scope, should_skip_path, urls_match

PRICE_RE = re.compile(
    r"(?:(?P<symbol>[$€£¥₹])\s*(?P<amount>\d{1,6}(?:[.,]\d{3})*(?:[.,]\d{2})?)"
    r"|(?P<amount2>\d{1,6}(?:[.,]\d{3})*(?:[.,]\d{2})?)\s*(?P<code>USD|EUR|GBP|INR|SEK|DKK|NOK|AUD|CAD|JPY|CHF))",
    re.IGNORECASE,
)
PRODUCT_URL_RE = re.compile(r"(/productpage\.|/product/|/products/|/p/|/dp/|/sku/|productpage)", re.IGNORECASE)
PAGINATION_QUERY = {"page", "offset", "start", "p"}
PAGINATION_TEXT = {"next", "next page", ">", "›", "»", "older"}
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
}
NAME_KEYS = ("name", "title", "productname", "product_name", "displayname", "display_name")
PRICE_KEYS = (
    "price",
    "saleprice",
    "listprice",
    "regularprice",
    "currentprice",
    "formattedprice",
    "amount",
    "value",
    "lowprice",
    "highprice",
    "whiteprice",
    "redprice",
    "pricevalue",
)
IMAGE_KEYS = ("image", "images", "imageurl", "image_url", "img", "src", "primaryimage", "gallery")
SKU_KEYS = ("sku", "articlecode", "articleid", "productid", "mpn", "productgroupid", "reference", "gtin", "gtin13")
URL_KEYS = ("url", "link", "href", "producturl", "pdpurl", "uri")
CURRENCY_KEYS = ("pricecurrency", "currency", "currencycode", "currency_code")
SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR"}
CODE_RE = re.compile(r"\b(USD|EUR|GBP|INR|SEK|DKK|NOK|AUD|CAD|JPY|CHF)\b", re.IGNORECASE)


@dataclass
class ExtractedImage:
    url: str
    alt: str | None = None


@dataclass
class ExtractedVariant:
    sku: str | None = None
    color: str | None = None
    size: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    availability: str | None = None


@dataclass
class ExtractedProduct:
    url: str | None
    name: str | None = None
    description: str | None = None
    brand: str | None = None
    sku: str | None = None
    price: Decimal | None = None
    compare_at_price: Decimal | None = None
    currency: str | None = None
    availability: str | None = None
    images: list[ExtractedImage] = field(default_factory=list)
    variants: list[ExtractedVariant] = field(default_factory=list)
    is_main: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Crumb:
    name: str
    url: str | None = None


@dataclass
class LinkOut:
    url: str
    name: str
    kind: str
    sort_order: int = 0


@dataclass
class PageExtract:
    is_product: bool
    heading: str | None
    breadcrumbs: list[Crumb]
    links: list[LinkOut]
    products: list[ExtractedProduct]


def clean_text(value: Any, limit: int = 4000) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text:
        return None
    return text[:limit]


def parse_decimal(amount: str) -> Decimal | None:
    cleaned = re.sub(r"[^\d,.\-]", "", amount.strip())
    if not cleaned or cleaned in {".", ",", "-"}:
        return None
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        if re.search(r",\d{2}$", cleaned):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    if value <= 0 or value > Decimal("1000000"):
        return None
    return value.quantize(Decimal("0.01"))


def parse_price_value(value: Any) -> tuple[Decimal | None, str | None]:
    if value is None or isinstance(value, bool):
        return None, None
    if isinstance(value, (int, float, Decimal)):
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            return None, None
        if number <= 0 or number > Decimal("1000000"):
            return None, None
        return number.quantize(Decimal("0.01")), None
    if isinstance(value, dict):
        amount = None
        for key in ("value", "amount", "price", "formatted", "formattedvalue", "raw"):
            found = ci_get(value, key)
            if found is not None and not isinstance(found, (dict, list)):
                amount = found
                break
        currency = clean_currency(ci_get(value, "currency", "pricecurrency", "currencycode"))
        if amount is None:
            return None, currency
        parsed, symbol_currency = parse_price_value(amount)
        return parsed, currency or symbol_currency
    text = str(value)
    match = PRICE_RE.search(text)
    if match:
        raw_amount = match.group("amount") or match.group("amount2")
        symbol = match.group("symbol")
        code = match.group("code")
        return parse_decimal(raw_amount), (code.upper() if code else SYMBOLS.get(symbol or ""))
    parsed = parse_decimal(text)
    code_match = CODE_RE.search(text)
    currency = code_match.group(1).upper() if code_match else None
    return parsed, currency


def clean_currency(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip().upper()
    if text in SYMBOLS:
        return SYMBOLS[text]
    if re.fullmatch(r"[A-Z]{3}", text):
        return text
    return None


def ci_get(data: dict[str, Any], *keys: str) -> Any:
    lookup = {str(key).lower(): key for key in data}
    for key in keys:
        original = lookup.get(key.lower())
        if original is not None:
            return data[original]
    return None


def normalize_availability(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    if "/" in text:
        text = text.rstrip("/").split("/")[-1]
    text = text.replace("_", " ").strip()
    return text[:64] or None


def make_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def meta_content(soup: BeautifulSoup, *names: str) -> str | None:
    wanted = {name.lower() for name in names}
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or tag.get("itemprop") or "").lower()
        if key in wanted:
            content = clean_text(tag.get("content"), 4000)
            if content:
                return content
    return None


def looks_like_product_url(url: str) -> bool:
    path = urlparse(url).path
    if PRODUCT_URL_RE.search(path):
        return True
    last = path.rstrip("/").split("/")[-1]
    return re.search(r"\d{5,}", last) is not None


def is_pagination(url: str, text: str) -> bool:
    query_keys = {key.lower() for key, _ in parse_qsl(urlparse(url).query, keep_blank_values=False)}
    if query_keys & PAGINATION_QUERY:
        return True
    return text.strip().lower() in PAGINATION_TEXT


def label_from_url(url: str) -> str | None:
    segment = unquote(urlparse(url).path.rstrip("/").split("/")[-1])
    segment = re.sub(r"\.(html?|php|aspx)$", "", segment, flags=re.IGNORECASE)
    segment = segment.replace("-", " ").replace("_", " ").strip()
    if not segment:
        return None
    if re.fullmatch(r"[a-z]{2}([_-][a-z]{2})?", segment, flags=re.IGNORECASE):
        return None
    return clean_text(segment, 200)


def usable_label(text: str | None) -> str | None:
    label = clean_text(text, 200)
    if not label:
        return None
    if label.lower() in GENERIC_NAMES or PRICE_RE.search(label):
        return None
    if len(label) < 2:
        return None
    return label


def types_of(node: dict[str, Any]) -> set[str]:
    raw = node.get("@type")
    if isinstance(raw, str):
        return {raw.lower()}
    if isinstance(raw, list):
        return {str(item).lower() for item in raw}
    return set()


def iter_ld_nodes(payload: Any) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        nodes.append(value)
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                walk(item)

    walk(payload)
    return nodes


def jsonld_payloads(soup: BeautifulSoup) -> list[Any]:
    payloads: list[Any] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        text = script.string or script.get_text() or ""
        text = text.strip()
        if not text:
            continue
        try:
            payloads.append(json.loads(text))
        except json.JSONDecodeError:
            continue
    return payloads


def string_value(value: Any) -> str | None:
    if isinstance(value, str):
        return clean_text(value, 500)
    if isinstance(value, dict):
        return clean_text(ci_get(value, "name", "url", "@id"), 500)
    return None


def image_values(value: Any, base_url: str) -> list[ExtractedImage]:
    images: list[ExtractedImage] = []
    if isinstance(value, str):
        url = absolutize(value, base_url)
        if url:
            images.append(ExtractedImage(url=url))
    elif isinstance(value, dict):
        candidate = ci_get(value, "url", "contenturl", "src", "@id")
        if isinstance(candidate, str):
            url = absolutize(candidate, base_url)
            if url:
                images.append(ExtractedImage(url=url, alt=clean_text(ci_get(value, "caption", "name"), 300)))
    elif isinstance(value, list):
        for item in value[:12]:
            images.extend(image_values(item, base_url))
    return images


def brand_value(value: Any) -> str | None:
    if isinstance(value, str):
        return clean_text(value, 255)
    if isinstance(value, dict):
        return clean_text(ci_get(value, "name"), 255)
    return None


def offer_parts(offer: Any) -> tuple[Decimal | None, Decimal | None, str | None, str | None, str | None, str | None]:
    """Return price, compare_at, currency, availability, sku, size."""
    if not isinstance(offer, dict):
        return None, None, None, None, None, None
    if "offers" in {key.lower() for key in offer} and "price" not in {key.lower() for key in offer}:
        return offer_parts(ci_get(offer, "offers"))
    price, currency = parse_price_value(ci_get(offer, "price", "lowprice", "amount"))
    high, high_currency = parse_price_value(ci_get(offer, "highprice"))
    currency = clean_currency(ci_get(offer, "pricecurrency", "currency")) or currency or high_currency
    availability = normalize_availability(ci_get(offer, "availability"))
    sku = clean_text(ci_get(offer, "sku", "mpn"), 128)
    size = clean_text(ci_get(offer, "size"), 128)
    compare_at = high if high and price and high > price else None
    return price, compare_at, currency, availability, sku, size


def product_from_ld(node: dict[str, Any], page_url: str) -> ExtractedProduct | None:
    name = clean_text(node.get("name"), 500)
    offers = node.get("offers")
    offer_nodes = offers if isinstance(offers, list) else ([offers] if isinstance(offers, dict) else [])
    price = compare_at = currency = availability = None
    variants: list[ExtractedVariant] = []
    for offer in offer_nodes:
        offer_price, offer_compare, offer_currency, offer_availability, offer_sku, offer_size = offer_parts(offer)
        price = price or offer_price
        compare_at = compare_at or offer_compare
        currency = currency or offer_currency
        availability = availability or offer_availability
        if offer_sku or offer_size:
            variants.append(
                ExtractedVariant(
                    sku=offer_sku,
                    size=offer_size,
                    price=offer_price,
                    currency=offer_currency,
                    availability=offer_availability,
                )
            )
    variants.extend(variants_from_node(node, page_url))
    images = image_values(node.get("image"), page_url)
    sku = clean_text(node.get("sku") or node.get("mpn") or node.get("productID") or node.get("productGroupID"), 128)
    url = string_value(node.get("url")) or string_value(node.get("@id"))
    absolute = canonicalize(url, page_url) if url else None
    if not name and not sku:
        return None
    description = node.get("description")
    if isinstance(description, str) and "<" in description:
        description = BeautifulSoup(description, "html.parser").get_text(" ", strip=True)
    return ExtractedProduct(
        url=absolute,
        name=name,
        description=clean_text(description, 4000),
        brand=brand_value(node.get("brand")),
        sku=sku,
        price=price,
        compare_at_price=compare_at,
        currency=currency,
        availability=availability,
        images=dedupe_images(images),
        variants=variants[:40],
        raw={"source": "jsonld", "type": sorted(types_of(node))},
    )


def variants_from_node(node: dict[str, Any], page_url: str) -> list[ExtractedVariant]:
    raw_variants = ci_get(node, "hasvariant", "variants")
    if not isinstance(raw_variants, list):
        sizes = ci_get(node, "size", "sizes")
        if isinstance(sizes, list):
            return [ExtractedVariant(size=clean_text(size, 128)) for size in sizes[:40] if clean_text(size, 128)]
        if isinstance(sizes, str):
            return [ExtractedVariant(size=clean_text(sizes, 128))]
        return []
    variants: list[ExtractedVariant] = []
    for item in raw_variants[:40]:
        if isinstance(item, str):
            label = clean_text(item, 128)
            if label:
                variants.append(ExtractedVariant(size=label))
            continue
        if not isinstance(item, dict):
            continue
        price, currency = parse_price_value(ci_get(item, "price"))
        offer = ci_get(item, "offers")
        if isinstance(offer, dict):
            offer_price, _, offer_currency, offer_availability, offer_sku, offer_size = offer_parts(offer)
            price = price or offer_price
            currency = currency or offer_currency
        else:
            offer_availability = normalize_availability(ci_get(item, "availability"))
            offer_sku = None
            offer_size = None
        color = clean_text(ci_get(item, "color", "colour"), 128)
        size = clean_text(ci_get(item, "size"), 128) or offer_size
        sku = clean_text(ci_get(item, "sku"), 128) or offer_sku
        if color or size or sku:
            variants.append(
                ExtractedVariant(
                    sku=sku,
                    color=color,
                    size=size,
                    price=price,
                    currency=currency,
                    availability=offer_availability,
                )
            )
    return variants


def breadcrumbs_from_ld(nodes: list[dict[str, Any]], page_url: str) -> list[Crumb]:
    crumbs: list[Crumb] = []
    for node in nodes:
        if "breadcrumblist" not in types_of(node):
            continue
        elements = node.get("itemListElement") or []
        if not isinstance(elements, list):
            continue
        ranked: list[tuple[int, Crumb]] = []
        for index, element in enumerate(elements):
            if not isinstance(element, dict):
                continue
            name = clean_text(element.get("name"), 200)
            item = element.get("item")
            url = None
            if isinstance(item, str):
                url = canonicalize(item, page_url)
                name = name or label_from_url(item)
            elif isinstance(item, dict):
                name = name or clean_text(item.get("name"), 200)
                url = canonicalize(string_value(item.get("@id") or item.get("url")) or "", page_url)
            position = element.get("position")
            try:
                rank = int(position)
            except (TypeError, ValueError):
                rank = index
            if name:
                ranked.append((rank, Crumb(name=name, url=url)))
        crumbs = [crumb for _, crumb in sorted(ranked, key=lambda pair: pair[0])]
    return crumbs


def listing_urls_from_ld(nodes: list[dict[str, Any]], page_url: str) -> list[str]:
    urls: list[str] = []
    for node in nodes:
        if "itemlist" not in types_of(node):
            continue
        elements = node.get("itemListElement") or []
        if not isinstance(elements, list):
            continue
        for element in elements:
            if isinstance(element, str):
                url = canonicalize(element, page_url)
            elif isinstance(element, dict):
                item = element.get("item") or element.get("url")
                if isinstance(item, dict):
                    item = item.get("url") or item.get("@id")
                url = canonicalize(str(item), page_url) if item else None
            else:
                url = None
            if url:
                urls.append(url)
    return urls


def looks_like_product_dict(node: dict[str, Any]) -> bool:
    name = None
    for key in NAME_KEYS:
        value = ci_get(node, key)
        if isinstance(value, str):
            name = clean_text(value, 180)
            break
    if not name or len(name) < 2:
        return False
    price, _ = first_price(node)
    if price is None:
        return False
    return bool(first_images(node, "https://example.invalid"))


def first_price(node: dict[str, Any]) -> tuple[Decimal | None, str | None]:
    for key in PRICE_KEYS:
        value = ci_get(node, key)
        if value is None or isinstance(value, list):
            continue
        price, currency = parse_price_value(value)
        if price is not None:
            explicit = clean_currency(ci_get(node, *CURRENCY_KEYS))
            return price, explicit or currency
    offers = ci_get(node, "offers", "priceinfo", "prices")
    if isinstance(offers, dict):
        return first_price(offers)
    if isinstance(offers, list) and offers and isinstance(offers[0], dict):
        return first_price(offers[0])
    return None, None


def first_images(node: dict[str, Any], base_url: str) -> list[ExtractedImage]:
    for key in IMAGE_KEYS:
        value = ci_get(node, key)
        if value:
            images = image_values(value, base_url)
            if images:
                return images
    return []


def collect_product_dicts(node: Any, found: list[dict[str, Any]], depth: int = 0) -> None:
    if depth > 8 or len(found) >= 300:
        return
    if isinstance(node, list):
        for item in node[:300]:
            collect_product_dicts(item, found, depth + 1)
        return
    if not isinstance(node, dict):
        return
    if looks_like_product_dict(node):
        found.append(node)
    for value in node.values():
        if isinstance(value, (dict, list)):
            collect_product_dicts(value, found, depth + 1)


def product_from_dict(node: dict[str, Any], page_url: str, source: str) -> ExtractedProduct | None:
    name = None
    for key in NAME_KEYS:
        value = ci_get(node, key)
        if isinstance(value, str):
            name = clean_text(value, 500)
            break
    price, currency = first_price(node)
    explicit_currency = clean_currency(ci_get(node, *CURRENCY_KEYS))
    currency = explicit_currency or currency
    sku = None
    for key in SKU_KEYS:
        value = ci_get(node, key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            sku = clean_text(value, 128)
            break
    url = None
    for key in URL_KEYS:
        value = ci_get(node, key)
        if isinstance(value, str):
            url = canonicalize(value, page_url)
            if url:
                break
    images = first_images(node, page_url)
    if not name or price is None or not images:
        return None
    if not url and not sku:
        return None
    compare_at, _ = parse_price_value(ci_get(node, "regularprice", "listprice", "originalprice", "wasprice"))
    if compare_at is not None and price is not None and compare_at <= price:
        compare_at = None
    description = ci_get(node, "description", "shortdescription")
    return ExtractedProduct(
        url=url,
        name=name,
        description=clean_text(description, 4000) if isinstance(description, str) else None,
        brand=brand_value(ci_get(node, "brand")),
        sku=sku,
        price=price,
        compare_at_price=compare_at,
        currency=currency,
        availability=normalize_availability(ci_get(node, "availability", "stock", "stockstate")),
        images=dedupe_images(images),
        variants=variants_from_node(node, page_url),
        raw={"source": source},
    )


def dedupe_images(images: list[ExtractedImage]) -> list[ExtractedImage]:
    seen: set[str] = set()
    result: list[ExtractedImage] = []
    for image in images:
        if not image.url or image.url.startswith("data:"):
            continue
        if image.url in seen:
            continue
        seen.add(image.url)
        result.append(image)
    return result[:12]


def merge_products(current: ExtractedProduct, extra: ExtractedProduct) -> ExtractedProduct:
    def pick(old: Any, new: Any) -> Any:
        return new if new not in (None, "", [], {}) else old

    current.url = pick(current.url, extra.url)
    current.name = pick(current.name, extra.name)
    current.description = extra.description if extra.description and (
        not current.description or len(extra.description) > len(current.description)
    ) else current.description
    current.brand = pick(current.brand, extra.brand)
    current.sku = pick(current.sku, extra.sku)
    current.price = pick(current.price, extra.price)
    current.compare_at_price = pick(current.compare_at_price, extra.compare_at_price)
    current.currency = pick(current.currency, extra.currency)
    current.availability = pick(current.availability, extra.availability)
    if extra.images:
        current.images = dedupe_images(current.images + extra.images)
    if extra.variants and not current.variants:
        current.variants = extra.variants
    current.is_main = current.is_main or extra.is_main
    return current


def dedupe_products(products: list[ExtractedProduct]) -> list[ExtractedProduct]:
    merged: dict[str, ExtractedProduct] = {}
    order: list[str] = []
    for product in products:
        key = product.url or (f"sku:{product.sku}" if product.sku else None)
        if key is None:
            continue
        if key not in merged:
            merged[key] = product
            order.append(key)
        else:
            merged[key] = merge_products(merged[key], product)
    return [merged[key] for key in order]


def product_from_meta(soup: BeautifulSoup, page_url: str) -> ExtractedProduct | None:
    og_type = (meta_content(soup, "og:type") or "").lower()
    price_text = meta_content(soup, "product:price:amount", "og:price:amount")
    if "product" not in og_type and not price_text:
        return None
    price, currency = parse_price_value(price_text) if price_text else (None, None)
    currency = clean_currency(meta_content(soup, "product:price:currency", "og:price:currency")) or currency
    image_url = meta_content(soup, "og:image")
    images = []
    if image_url:
        absolute = absolutize(image_url, page_url)
        if absolute:
            images.append(ExtractedImage(url=absolute))
    name = meta_content(soup, "og:title") or meta_content(soup, "twitter:title")
    return ExtractedProduct(
        url=page_url,
        name=name,
        description=meta_content(soup, "og:description", "description"),
        brand=meta_content(soup, "product:brand", "og:brand"),
        sku=meta_content(soup, "product:retailer_item_id", "sku"),
        price=price,
        currency=currency,
        availability=normalize_availability(meta_content(soup, "product:availability")),
        images=images,
        is_main=True,
        raw={"source": "meta"},
    )


def card_product(node: Tag, page_url: str, host: str, prefix: str) -> ExtractedProduct | None:
    anchors = [anchor for anchor in node.find_all("a", href=True) if isinstance(anchor, Tag)]
    if not anchors or len(anchors) > 12:
        return None
    chosen = None
    chosen_url = None
    for anchor in anchors:
        url = canonicalize(str(anchor.get("href")), page_url)
        if not url or not in_scope(url, host, prefix) or should_skip_path(url):
            continue
        if urls_match(url, page_url):
            continue
        chosen = anchor
        chosen_url = url
        if looks_like_product_url(url):
            break
    if chosen is None or chosen_url is None:
        return None
    text = node.get_text(" ", strip=True)
    price, currency = parse_price_value(text) if PRICE_RE.search(text) else (None, None)
    sku = None
    for attr in ("data-sku", "data-product-id", "data-productid", "data-articlecode", "data-article-code"):
        if node.get(attr):
            sku = clean_text(node.get(attr), 128)
            break
    if price is None and not sku and not looks_like_product_url(chosen_url):
        return None
    image = image_from_node(node, page_url)
    name = usable_label(chosen.get_text(" ", strip=True)) or label_from_url(chosen_url)
    return ExtractedProduct(
        url=chosen_url,
        name=name,
        sku=sku,
        price=price,
        currency=currency,
        images=[image] if image else [],
        raw={"source": "dom"},
    )


def image_from_node(node: Tag, page_url: str) -> ExtractedImage | None:
    image = node.find("img")
    if not isinstance(image, Tag):
        return None
    candidate = None
    for attr in ("src", "data-src", "data-original", "data-lazy", "data-image"):
        if image.get(attr):
            candidate = str(image.get(attr))
            break
    if candidate is None and image.get("srcset"):
        candidate = str(image.get("srcset")).split(",")[0].strip().split(" ")[0]
    if not candidate or candidate.startswith("data:"):
        return None
    url = absolutize(candidate, page_url)
    if not url:
        return None
    return ExtractedImage(url=url, alt=clean_text(image.get("alt"), 300))


def content_root(soup: BeautifulSoup) -> Tag:
    main = soup.select_one("main") or soup.select_one("[role=main]")
    if isinstance(main, Tag):
        return main
    body = soup.body
    return body if isinstance(body, Tag) else soup


def anchor_inside(node: Tag, names: tuple[str, ...]) -> bool:
    return node.find_parent(list(names)) is not None


def collect_links(
    soup: BeautifulSoup,
    page_url: str,
    host: str,
    prefix: str,
    is_home: bool,
    product_urls: set[str],
) -> list[LinkOut]:
    links: list[LinkOut] = []
    seen: set[str] = set()

    def add(href: str, text: str, kind: str) -> None:
        url = canonicalize(href, page_url)
        if not url or url in seen or urls_match(url, page_url):
            return
        if not in_scope(url, host, prefix) or should_skip_path(url):
            return
        label = usable_label(text) or label_from_url(url) or ""
        if kind != "pagination" and not label:
            return
        if is_pagination(url, text):
            kind = "pagination"
            label = label or "Next"
        seen.add(url)
        links.append(LinkOut(url=url, name=label or "Next", kind=kind, sort_order=len(links)))

    if is_home:
        for anchor in soup.select("header a[href], nav a[href], [role=navigation] a[href]"):
            if not isinstance(anchor, Tag) or anchor_inside(anchor, ("footer",)):
                continue
            add(str(anchor.get("href")), anchor.get_text(" ", strip=True), "category")

    root = content_root(soup)
    for anchor in root.find_all("a", href=True):
        if not isinstance(anchor, Tag):
            continue
        if anchor_inside(anchor, ("header", "footer", "nav")):
            continue
        href = str(anchor.get("href"))
        url = canonicalize(href, page_url)
        text = anchor.get_text(" ", strip=True)
        if not url:
            continue
        kind = "product" if url in product_urls or looks_like_product_url(url) else "category"
        parent = anchor.find_parent(["article", "li"])
        if isinstance(parent, Tag) and PRICE_RE.search(parent.get_text(" ", strip=True)):
            kind = "product"
        add(href, text, kind)

    if is_home and not any(link.kind == "category" for link in links):
        for anchor in soup.find_all("a", href=True):
            if not isinstance(anchor, Tag) or anchor_inside(anchor, ("footer",)):
                continue
            href = str(anchor.get("href"))
            url = canonicalize(href, page_url)
            kind = "product" if url and (url in product_urls or looks_like_product_url(url)) else "category"
            add(href, anchor.get_text(" ", strip=True), kind)

    categories = [link for link in links if link.kind == "category"][:150]
    products = [link for link in links if link.kind == "product"][:250]
    pages = [link for link in links if link.kind == "pagination"][:20]
    ordered = categories + pages + products
    for index, link in enumerate(ordered):
        link.sort_order = index
    return ordered


def dom_breadcrumbs(soup: BeautifulSoup, page_url: str) -> list[Crumb]:
    container = soup.select_one("[aria-label*=breadcrumb i], ol.breadcrumb, nav.breadcrumb, .breadcrumbs")
    if not isinstance(container, Tag):
        return []
    crumbs: list[Crumb] = []
    for anchor in container.find_all("a", href=True):
        if not isinstance(anchor, Tag):
            continue
        name = usable_label(anchor.get_text(" ", strip=True)) or label_from_url(str(anchor.get("href")))
        if not name:
            continue
        crumbs.append(Crumb(name=name, url=canonicalize(str(anchor.get("href")), page_url)))
    return crumbs


def heading_of(soup: BeautifulSoup) -> str | None:
    heading = soup.find("h1")
    if not isinstance(heading, Tag):
        return None
    return usable_label(heading.get_text(" ", strip=True))


def extract_page(
    html: str,
    page_url: str,
    json_payloads: list[Any],
    *,
    host: str,
    prefix: str,
    is_home: bool,
) -> PageExtract:
    soup = make_soup(html)
    nodes: list[dict[str, Any]] = []
    for payload in jsonld_payloads(soup):
        nodes.extend(iter_ld_nodes(payload))

    main_products: list[ExtractedProduct] = []
    for node in nodes:
        kinds = types_of(node)
        if kinds & {"product", "productgroup"}:
            product = product_from_ld(node, page_url)
            if product:
                main_products.append(product)

    listed_urls = listing_urls_from_ld(nodes, page_url)
    meta_product = product_from_meta(soup, page_url)
    og_type = (meta_content(soup, "og:type") or "").lower()
    matched_page = any(urls_match(product.url, page_url) for product in main_products)
    is_product = bool(
        "product" in og_type
        or meta_content(soup, "product:price:amount", "product:retailer_item_id")
        or matched_page
        or (main_products and len(listed_urls) <= 1)
    )
    # A long item list is a category grid, even if the page also advertises one product.
    if len(listed_urls) > 1 and not matched_page:
        is_product = False

    products: list[ExtractedProduct] = []
    if is_product:
        if main_products:
            main = main_products[0]
            for extra in main_products[1:]:
                main = merge_products(main, extra)
            if meta_product:
                main = merge_products(main, meta_product)
            if not main.url:
                main.url = page_url
            main.is_main = True
            products.append(main)
        elif meta_product:
            products.append(meta_product)
    else:
        for url in listed_urls:
            products.append(ExtractedProduct(url=url, raw={"source": "itemlist"}))
        products.extend(main_products)

    for node in content_root(soup).select(
        "article, li, [data-sku], [data-product-id], [data-productid], [data-articlecode], [class*=product]"
    ):
        if not isinstance(node, Tag):
            continue
        card = card_product(node, page_url, host, prefix)
        if card:
            products.append(card)

    for payload in json_payloads:
        dicts: list[dict[str, Any]] = []
        collect_product_dicts(payload, dicts)
        for item in dicts:
            product = product_from_dict(item, page_url, "payload")
            if product and product.url and in_scope(product.url, host, prefix):
                products.append(product)
            elif product and product.sku and not product.url:
                products.append(product)

    products = [
        product
        for product in dedupe_products(products)
        if product.url is None or (in_scope(product.url, host, prefix) and not should_skip_path(product.url))
    ]
    if is_product and products and not any(product.is_main for product in products):
        products[0].is_main = True

    product_urls = {product.url for product in products if product.url}
    product_urls.update(url for url in listed_urls if in_scope(url, host, prefix))
    crumbs = breadcrumbs_from_ld(nodes, page_url) or dom_breadcrumbs(soup, page_url)
    links = collect_links(soup, page_url, host, prefix, is_home, product_urls)
    return PageExtract(
        is_product=is_product,
        heading=heading_of(soup),
        breadcrumbs=crumbs,
        links=links,
        products=products,
    )
