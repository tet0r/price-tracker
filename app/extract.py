"""Pull price, title, image and availability out of a product page.

Each field has a strategy: "auto" (schema.org JSON-LD, then meta tags, then
heuristics), "schema" (JSON-LD only), "selector" (CSS, `sel|attr` for an
attribute), "regex" (first capture group, run on the raw body) or "json"
(dot path into a JSON response).
"""
import json
import re
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup

FIELDS = ("title", "price", "image", "availability")
UNAVAILABLE = ("out_of_stock", "discontinued")


@dataclass
class Extracted:
    price: float | None = None
    currency: str | None = None
    name: str | None = None
    image: str | None = None
    availability: str | None = None
    source: str | None = None


ISO_CODES = (
    "USD EUR GBP JPY CAD AUD CHF CNY INR SEK NOK DKK PLN CZK HUF BRL MXN NZD "
    "SGD HKD KRW TRY ZAR AED ILS RON"
).split()
ISO_RE = re.compile(r"\b(" + "|".join(ISO_CODES) + r")\b")
# Longest symbols first so "CA$" wins over "$"
SYMBOLS = [
    ("CA$", "CAD"), ("AU$", "AUD"), ("NZ$", "NZD"), ("US$", "USD"), ("C$", "CAD"),
    ("A$", "AUD"), ("R$", "BRL"), ("$", "USD"), ("€", "EUR"), ("£", "GBP"),
    ("¥", "JPY"), ("₹", "INR"), ("₩", "KRW"), ("₺", "TRY"), ("₪", "ILS"),
    ("zł", "PLN"), ("Kč", "CZK"),
]

# Either digit groups with thousands separators or plain digits, then an
# optional 1-2 digit decimal part: "1,299.99", "1.299,99", "1 299,99", "19.99"
NUM_RE = re.compile(
    r"(?P<int>\d{1,3}(?:[   ',.]\d{3})+|\d+)(?:[.,](?P<dec>\d{1,2}))?(?!\d)"
)
OLD_PRICE_RE = re.compile(
    r"\b(old|was|previous|original|compare|strike\w*|crossed|rrp|msrp)\b"
    r"|list-?price|text-price|line-through",
    re.I,
)
# Checked in order, so "unavailable" is tested before "available"
AVAILABILITY_PATTERNS = [
    ("discontinued", r"discontinued"),
    ("out_of_stock", r"out\s*of\s*stock|sold\s*out|unavailable|not\s+available"),
    ("pre_order", r"pre-?\s*order|pre-?\s*sale"),
    ("back_order", r"back-?\s*order"),
    ("in_stock", r"in\s*stock|available|limited\s*availability|online\s*only|in\s*store\s*only|add to (cart|basket|bag)"),
]

PRICE_SELECTORS = [
    # (selector, needs a currency symbol/code in the text to be trusted)
    ("[itemprop=price]", False),
    ("#corePriceDisplay_desktop_feature_div .a-offscreen", False),
    ("#corePrice_feature_div .a-offscreen", False),
    (".a-price .a-offscreen", True),
    ("[data-testid*=price i]", True),
    ("[data-test*=price i]", True),
    ("[class*=current-price i]", True),
    ("[class*=sale-price i]", True),
    ("[class*=product-price i]", True),
    ("[class*=price i]", True),
    ("[id*=price i]", True),
]


def detect_currency(text: str) -> str | None:
    m = ISO_RE.search(text)
    if m:
        return m.group(1)
    for symbol, code in SYMBOLS:
        if symbol in text:
            return code
    return None


def parse_price(value) -> tuple[float | None, str | None]:
    """Return (price, currency) from a number or a human-formatted string."""
    if value is None or isinstance(value, bool):
        return None, None
    if isinstance(value, (int, float)):
        return (float(value) if value > 0 else None), None
    text = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        number = float(text)
        return (number if number > 0 else None), None
    currency = detect_currency(text)
    m = NUM_RE.search(text)
    if not m:
        return None, currency
    number = float(re.sub(r"\D", "", m["int"]) + "." + (m["dec"] or "0"))
    return (number if number > 0 else None), currency


def map_availability(text) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    for status, pattern in AVAILABILITY_PATTERNS:
        if re.search(pattern, text, re.I):
            return status
    return None


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _image_url(value) -> str | None:
    for item in _as_list(value):
        if isinstance(item, str):
            return item
        if isinstance(item, dict) and isinstance(item.get("url"), str):
            return item["url"]
    return None


def _offer_price(offer: dict) -> tuple[float | None, str | None]:
    currency = offer.get("priceCurrency")
    candidates = [offer.get("price"), offer.get("lowPrice")]
    for spec in _as_list(offer.get("priceSpecification")):
        if isinstance(spec, dict):
            candidates.append(spec.get("price"))
            currency = currency or spec.get("priceCurrency")
    for candidate in candidates:
        price, detected = parse_price(candidate)
        if price:
            return price, currency if isinstance(currency, str) else detected
    return None, None


def _from_jsonld(soup: BeautifulSoup) -> Extracted:
    ex = Extracted()
    for script in soup.find_all("script", type=re.compile(r"ld\+json", re.I)):
        try:
            data = json.loads(script.get_text(), strict=False)
        except ValueError:
            continue
        for node in _walk(data):
            if "offers" not in node:
                continue
            ex.name = ex.name or (node.get("name") if isinstance(node.get("name"), str) else None)
            ex.image = ex.image or _image_url(node.get("image"))
            for offer in _as_list(node["offers"]):
                if not isinstance(offer, dict):
                    continue
                ex.availability = ex.availability or map_availability(offer.get("availability"))
                price, currency = _offer_price(offer)
                if price:
                    ex.price, ex.currency, ex.source = price, currency, "json-ld"
                    ex.availability = map_availability(offer.get("availability")) or ex.availability
                    return ex
    return ex


def _meta(soup: BeautifulSoup, *keys: str) -> str | None:
    for key in keys:
        for attr in ("property", "name", "itemprop"):
            tag = soup.find("meta", attrs={attr: key})
            if tag and tag.get("content", "").strip():
                return tag["content"].strip()
    return None


def _from_meta(soup: BeautifulSoup) -> Extracted:
    ex = Extracted(
        name=_meta(soup, "og:title", "twitter:title"),
        image=_meta(soup, "og:image", "og:image:url", "twitter:image"),
        currency=_meta(soup, "product:price:currency", "og:price:currency", "priceCurrency"),
        availability=map_availability(_meta(soup, "product:availability", "og:availability", "availability")),
    )
    if not ex.name:
        heading = soup.title or soup.find("h1")
        ex.name = heading.get_text(strip=True) if heading else None
    if not ex.availability:
        link = soup.select_one("link[itemprop=availability]")
        ex.availability = map_availability(link.get("href")) if link else None
    raw = _meta(soup, "product:price:amount", "og:price:amount", "product:sale_price:amount")
    price, detected = parse_price(raw)
    if price:
        ex.price, ex.source = price, "meta"
        ex.currency = ex.currency or detected
    return ex


def _looks_like_old_price(el) -> bool:
    node = el
    for _ in range(3):
        if node is None or node.name in (None, "body", "html"):
            break
        if node.name in ("del", "s", "strike"):
            return True
        marks = " ".join(node.get("class") or []) + " " + (node.get("style") or "")
        if OLD_PRICE_RE.search(marks):
            return True
        node = node.parent
    return False


def _element_text(el, skip_old: bool = False) -> str:
    attr = el.get("content") or el.get("data-price")
    if attr:
        return attr
    if not skip_old:
        return el.get_text(" ", strip=True)
    # A price container often wraps both the struck-through and current price
    parts = (s.strip() for s in el.find_all(string=True) if not _looks_like_old_price(s.parent))
    return " ".join(p for p in parts if p)


def _from_heuristics(soup: BeautifulSoup) -> Extracted:
    for selector, needs_currency in PRICE_SELECTORS:
        for el in soup.select(selector, limit=25):
            if _looks_like_old_price(el):
                continue
            text = _element_text(el, skip_old=True)
            if not text or len(text) > 40:
                continue
            price, currency = parse_price(text)
            if price and (currency or not needs_currency):
                return Extracted(price=price, currency=currency, source="heuristic")
    return Extracted()


def _by_selector(soup: BeautifulSoup, value: str):
    selector, _, attr = (part.strip() for part in value.partition("|"))
    try:
        el = soup.select_one(selector)
    except Exception:
        return None
    if el is None:
        return None
    if attr:
        return el.get(attr) or el.get(f"data-{attr}")
    return el.get("src") if el.name == "img" else _element_text(el)


def _by_regex(body: str, value: str):
    # Accept PHP-style delimiters such as ~pattern~ or /pattern/
    if len(value) > 2 and value[0] == value[-1] and value[0] in "~/#@":
        value = value[1:-1]
    try:
        m = re.search(value, body, re.S)
    except re.error:
        return None
    if not m:
        return None
    return m.group(1) if m.groups() else m.group(0)


def _by_json_path(body: str, value: str):
    try:
        node = json.loads(body)
    except ValueError:
        return None
    for key in filter(None, value.split(".")):
        if isinstance(node, list) and key.lstrip("-").isdigit() and -len(node) <= int(key) < len(node):
            node = node[int(key)]
        elif isinstance(node, dict) and key in node:
            node = node[key]
        else:
            return None
    return node if isinstance(node, (str, int, float)) else None


def extract(body: str, url: str, strategies: dict | None = None) -> Extracted:
    soup = BeautifulSoup(body, "lxml")
    jsonld, meta = _from_jsonld(soup), _from_meta(soup)
    out = Extracted()

    def resolve(field: str):
        """Return ("auto" | "schema", None) or ("custom", raw_value)."""
        strategy = (strategies or {}).get(field) or {}
        kind, value = strategy.get("type") or "auto", (strategy.get("value") or "").strip()
        if kind in ("auto", "schema") or not value:
            return ("schema" if kind == "schema" else "auto"), None
        finder = {"selector": lambda: _by_selector(soup, value), "regex": lambda: _by_regex(body, value),
                  "json": lambda: _by_json_path(body, value)}[kind]
        return "custom", finder()

    kind, raw = resolve("price")
    if kind == "custom":
        out.price, out.currency = parse_price(raw)
        out.source = "custom"
    else:
        candidates = (jsonld,) if kind == "schema" else (jsonld, meta)
        found = next((c for c in candidates if c.price), None)
        if not found and kind == "auto":
            found = _from_heuristics(soup)
        if found:
            out.price, out.currency, out.source = found.price, found.currency, found.source
    out.currency = jsonld.currency or meta.currency or out.currency

    kind, raw = resolve("title")
    name = raw if kind == "custom" else jsonld.name if kind == "schema" else jsonld.name or meta.name
    out.name = " ".join(str(name).split())[:300] if name else None

    kind, raw = resolve("image")
    image = raw if kind == "custom" else jsonld.image if kind == "schema" else jsonld.image or meta.image
    image = urljoin(url, str(image)) if image else None
    out.image = image if image and image.startswith(("http://", "https://")) else None

    kind, raw = resolve("availability")
    if kind == "custom":
        out.availability = map_availability(str(raw)) if raw is not None else None
    else:
        out.availability = jsonld.availability or (meta.availability if kind == "auto" else None)
    return out
