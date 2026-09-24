"""
Pull product name, price, sale price and stock status out of a fetched page.

Strategy, in order. The first layer that yields a name and a price wins:

  1. JSON-LD  - the schema.org/Product block most shops publish for Google.
                Standard across sites, so it needs no per-site configuration.
  2. Microdata - the older itemprop="price" style of the same idea.
  3. Meta tags - OpenGraph / product meta. Usually name + price only.
  4. CSS rules - per-site selectors from selectors.json, for shops that
                publish none of the above. This layer uses Scrapling's
                adaptive mode so the selectors survive a site redesign.

Anything we cannot find comes back as None rather than a guess, so a blank
cell in the sheet always means "not found" and never "found, but wrong".
"""

import json
import re
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.parse import urlparse


@dataclass
class Product:
    name: Optional[str] = None
    price: Optional[float] = None
    sale_price: Optional[float] = None
    currency: Optional[str] = None
    in_stock: Optional[bool] = None
    source: str = "none"          # which layer produced the result
    error: Optional[str] = None

    def as_dict(self):
        return asdict(self)


# --------------------------------------------------------------- helpers

_IN_STOCK_WORDS = (
    "instock", "in stock", "available", "limitedavailability",
    "onlineonly", "instoreonly", "presale", "preorder", "backorder",
)
_OUT_WORDS = (
    "outofstock", "out of stock", "sold out", "soldout", "unavailable",
    "discontinued", "nostock",
)


def parse_price(raw) -> Optional[float]:
    """
    Turn a price as written by a website into a number.

    Handles '£51.77', '$1,234.56', '1.234,56' (European), '19.99 USD' and
    bare numbers. Returns None if there is no sensible number in there.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)

    text = str(raw).strip()
    if not text:
        return None

    # Keep only digits and separators.
    cleaned = re.sub(r"[^\d.,]", "", text)
    if not cleaned:
        return None

    has_dot, has_comma = "." in cleaned, "," in cleaned

    if has_dot and has_comma:
        # Whichever separator comes last is the decimal point.
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")   # 1.234,56
        else:
            cleaned = cleaned.replace(",", "")                      # 1,234.56
    elif has_comma:
        # A comma with exactly two digits after it is a decimal comma;
        # otherwise it is a thousands separator.
        if re.search(r",\d{2}$", cleaned):
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif has_dot:
        # '1.234' with three trailing digits is thousands, not decimal.
        if re.search(r"\.\d{3}$", cleaned) and cleaned.count(".") == 1:
            cleaned = cleaned.replace(".", "")

    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value > 0 else None


def parse_availability(raw) -> Optional[bool]:
    """Map a schema.org availability value or free text to True/False/None."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, dict):
        raw = raw.get("@id") or raw.get("name") or ""
    if isinstance(raw, list):
        raw = raw[0] if raw else ""

    text = str(raw).lower().strip()
    if not text:
        return None
    # 'https://schema.org/InStock' -> 'instock'
    text = text.rsplit("/", 1)[-1].replace("_", "").replace("-", "")

    for word in _OUT_WORDS:               # check 'out of stock' before 'stock'
        if word.replace(" ", "") in text.replace(" ", ""):
            return False
    for word in _IN_STOCK_WORDS:
        if word.replace(" ", "") in text.replace(" ", ""):
            return True
    return None


def _walk(node):
    """Yield every dict nested anywhere inside a parsed JSON structure."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _is_product(node) -> bool:
    types = node.get("@type")
    if isinstance(types, str):
        types = [types]
    if not isinstance(types, list):
        return False
    return any("product" in str(t).lower() for t in types)


# --------------------------------------------------------- layer 1: JSON-LD

def from_json_ld(page) -> Optional[Product]:
    blocks = page.css('script[type="application/ld+json"]::text').getall()
    for block in blocks:
        try:
            parsed = json.loads(block.strip())
        except (json.JSONDecodeError, AttributeError):
            continue

        for node in _walk(parsed):
            if not _is_product(node):
                continue

            name = node.get("name")
            if isinstance(name, dict):
                name = name.get("@value")
            if not name:
                continue

            product = Product(name=str(name).strip(), source="json-ld")

            offers = node.get("offers")
            offer_nodes = [o for o in _walk(offers) if isinstance(o, dict)] if offers else []

            prices = []
            for offer in offer_nodes:
                if product.currency is None:
                    product.currency = offer.get("priceCurrency")
                if product.in_stock is None:
                    product.in_stock = parse_availability(offer.get("availability"))

                for key in ("price", "lowPrice", "highPrice"):
                    value = parse_price(offer.get(key))
                    if value is not None:
                        prices.append(value)

                spec = offer.get("priceSpecification")
                for s in (_walk(spec) if spec else []):
                    if isinstance(s, dict):
                        value = parse_price(s.get("price"))
                        if value is not None:
                            prices.append(value)

            if prices:
                # When a page advertises several prices, the highest is the
                # list price and the lowest is what you would actually pay.
                high, low = max(prices), min(prices)
                product.price = high
                if low < high:
                    product.sale_price = low

            if product.price is not None or product.in_stock is not None:
                return product
    return None


# ------------------------------------------------------- layer 2: microdata

def _itemprop(page, prop):
    """
    Read a microdata property.

    The content attribute is authoritative when present, because it holds the
    machine-readable form ('19.99') while the visible text may be decorated
    ('Only $19.99!'). Falling back to the element's full text covers shops
    that omit the attribute.
    """
    value = page.css(f'[itemprop="{prop}"]::attr(content)').get()
    if value and value.strip():
        return value.strip()
    elements = page.css(f'[itemprop="{prop}"]')
    return _element_text(elements[0]) if elements else None


def from_microdata(page) -> Optional[Product]:
    name = _itemprop(page, "name")
    price_raw = _itemprop(page, "price")
    if not name or price_raw is None:
        return None

    return Product(
        name=name.strip(),
        price=parse_price(price_raw),
        currency=_itemprop(page, "priceCurrency"),
        in_stock=parse_availability(
            page.css('[itemprop="availability"]::attr(href)').get()
            or _itemprop(page, "availability")
        ),
        source="microdata",
    )


# ------------------------------------------------------- layer 3: meta tags

def from_meta(page) -> Optional[Product]:
    name = (
        page.css('meta[property="og:title"]::attr(content)').get()
        or page.css("title::text").get()
    )
    price_raw = (
        page.css('meta[property="product:price:amount"]::attr(content)').get()
        or page.css('meta[property="og:price:amount"]::attr(content)').get()
    )
    if not name:
        return None
    price = parse_price(price_raw)
    if price is None:
        return None

    return Product(
        name=name.strip(),
        price=price,
        currency=(
            page.css('meta[property="product:price:currency"]::attr(content)').get()
            or page.css('meta[property="og:price:currency"]::attr(content)').get()
        ),
        in_stock=parse_availability(
            page.css('meta[property="product:availability"]::attr(content)').get()
            or page.css('meta[property="og:availability"]::attr(content)').get()
        ),
        source="meta",
    )


# ------------------------------------------------- layer 4: CSS per site

def _element_text(element) -> Optional[str]:
    """
    Get the full visible text of an element, including its children.

    This has to be get_all_text() and not .text: .text returns only the
    element's own direct text node. A price written as
    <span class="price"><span>$</span>19.99</span> has an empty direct text
    node, so .text would return nothing at all. Shops nest markup like that
    constantly, so reading children is the normal case, not the exception.
    """
    for accessor in ("get_all_text", "text_content"):
        method = getattr(element, accessor, None)
        if callable(method):
            try:
                value = method()
            except Exception:
                continue
            if value and str(value).strip():
                return str(value).strip()

    value = getattr(element, "text", None)
    if value and str(value).strip():
        return str(value).strip()
    return None


def _text_via(page, selector):
    """
    Read one value using a site-specific selector.

    adaptive=True lets Scrapling relocate the element from its saved
    fingerprint if the site has been redesigned and the selector no longer
    matches. auto_save=True records that fingerprint whenever it does match,
    which is what makes the relocation possible on a later run. Both are
    ignored unless adaptive was enabled when the page was fetched, so the
    plain lookup below is the fallback rather than an error path.
    """
    if not selector:
        return None

    found = None
    try:
        found = page.css(selector, adaptive=True, auto_save=True)
    except Exception:
        pass
    if not found:
        try:
            found = page.css(selector)
        except Exception:
            return None
    if not found:
        return None

    return _element_text(found[0])


def from_css(page, rules) -> Optional[Product]:
    if not rules:
        return None

    name = _text_via(page, rules.get("name"))
    price = parse_price(_text_via(page, rules.get("price")))
    sale = parse_price(_text_via(page, rules.get("sale_price")))
    stock_text = _text_via(page, rules.get("in_stock"))

    if not name and price is None:
        return None

    # If the "sale" figure is not actually lower, treat it as the only price.
    if price is not None and sale is not None and sale >= price:
        sale = None
    if price is None and sale is not None:
        price, sale = sale, None

    in_stock = parse_availability(stock_text)
    if in_stock is None and stock_text:
        in_stock = None

    return Product(
        name=name,
        price=price,
        sale_price=sale,
        in_stock=in_stock,
        source="css",
    )


# ------------------------------------------------------------- entry point

def extract(page, url, selector_rules=None) -> Product:
    """Run each layer in turn and return the first usable result."""
    host = urlparse(url).netloc.lower().removeprefix("www.")
    rules = (selector_rules or {}).get(host)

    # A site with hand-written rules uses them first: they were added
    # precisely because the automatic layers got it wrong.
    layers = []
    if rules:
        layers.append(lambda: from_css(page, rules))
    layers += [
        lambda: from_json_ld(page),
        lambda: from_microdata(page),
        lambda: from_meta(page),
    ]
    if not rules:
        layers.append(lambda: from_css(page, rules))

    best = None
    for layer in layers:
        try:
            result = layer()
        except Exception:
            continue
        if not result:
            continue
        if result.name and result.price is not None:
            return result                      # complete answer, stop here
        best = best or result                  # partial: keep looking

    return best or Product(error="no product data found on page")
