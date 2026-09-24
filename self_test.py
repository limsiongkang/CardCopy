"""
Self-test. Run this any time you change something, or if results look wrong:

    run_monitor.bat --help      (no - that runs the monitor)
    %USERPROFILE%\\.venvs\\cardcopy\\Scripts\\python.exe self_test.py

It uses made-up data and never touches the network, Google Sheets or email,
so it is always safe to run.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scrapling import Selector

from extractor import extract, parse_price, parse_availability
from monitor import find_changes, effective_price

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL  {label}: got {got!r} want {want!r}")
    else:
        print(f"  ok    {label}")


def page_with(payload):
    body = json.dumps(payload)
    return Selector(
        f'<html><head><script type="application/ld+json">{body}</script>'
        f"</head><body></body></html>"
    )


def test_prices():
    print("\n[price parsing]")
    for raw, want in [
        ("£51.77", 51.77), ("$1,234.56", 1234.56), ("1.234,56 €", 1234.56),
        ("19.99 USD", 19.99), ("1,299", 1299.0), ("1.299", 1299.0),
        ("0,99", 0.99), ("EUR 45,00", 45.0), ("From $12.34", 12.34),
        ("1,234,567.89", 1234567.89), ("$0.00", None), ("", None),
        (None, None), ("Sold Out", None), (29.99, 29.99),
    ]:
        check(f"parse_price({raw!r})", parse_price(raw), want)


def test_availability():
    print("\n[availability parsing]")
    for raw, want in [
        ("https://schema.org/InStock", True),
        ("https://schema.org/OutOfStock", False),
        ("In stock (22 available)", True), ("Sold out", False),
        ("http://schema.org/LimitedAvailability", True),
        ({"@id": "https://schema.org/OutOfStock"}, False),
        ("https://schema.org/PreOrder", True),
        ("", None), (None, None), ("banana", None),
    ]:
        check(f"parse_availability({str(raw)[:32]!r})", parse_availability(raw), want)


def test_json_ld():
    print("\n[json-ld extraction]")
    p = extract(page_with({
        "@type": "Product", "name": "Widget A",
        "offers": {"price": "19.99", "priceCurrency": "USD",
                   "availability": "https://schema.org/InStock"}}), "https://s.example/a")
    check("simple name", p.name, "Widget A")
    check("simple price", p.price, 19.99)
    check("simple no sale", p.sale_price, None)
    check("simple in stock", p.in_stock, True)

    p = extract(page_with({"@graph": [
        {"@type": "WebPage", "name": "nope"},
        {"@type": "Product", "name": "Widget B",
         "offers": {"price": 45.0, "priceCurrency": "GBP",
                    "availability": "http://schema.org/OutOfStock"}}]}), "https://s.example/b")
    check("@graph name", p.name, "Widget B")
    check("@graph out of stock", p.in_stock, False)

    p = extract(page_with([
        {"@type": "BreadcrumbList"},
        {"@type": "Product", "name": "Widget C",
         "offers": {"@type": "AggregateOffer", "lowPrice": "80.00",
                    "highPrice": "100.00", "availability": "InStock"}}]), "https://s.example/c")
    check("aggregate list price", p.price, 100.0)
    check("aggregate sale price", p.sale_price, 80.0)

    p = extract(page_with({"@type": "Product", "name": "Widget D", "offers": [
        {"price": "10.00"}, {"price": "10.00"}]}), "https://s.example/d")
    check("equal offers invent no sale", p.sale_price, None)

    bad = Selector('<html><head><script type="application/ld+json">{bad,,</script></head></html>')
    check("malformed json survives", extract(bad, "https://s.example/e").error is not None, True)


def test_nested_text():
    print("\n[nested markup]")
    html = (
        '<html><body>'
        '<h1 class="t">Nested Widget</h1>'
        '<div class="p"><span>$</span>19.99</div>'
        '<div class="s"><i class="icon"></i>In stock (4 available)</div>'
        "</body></html>"
    )
    rules = {"s.example": {"name": ".t", "price": ".p", "in_stock": ".s"}}
    p = extract(Selector(html), "https://s.example/n", rules)
    check("name through css", p.name, "Nested Widget")
    check("price inside nested span", p.price, 19.99)
    check("stock after nested icon", p.in_stock, True)


def test_changes():
    print("\n[change detection]")
    check("effective price prefers sale",
          effective_price({"price": 100.0, "sale_price": 80.0}), 80.0)
    check("effective price without sale",
          effective_price({"price": 100.0, "sale_price": None}), 100.0)

    prev = {
        "u/drop": {"price": 100.0, "sale_price": None, "in_stock": True},
        "u/oos": {"price": 50.0, "sale_price": None, "in_stock": True},
        "u/restock": {"price": 50.0, "sale_price": None, "in_stock": False},
        "u/rise": {"price": 20.0, "sale_price": None, "in_stock": True},
        "u/unknown": {"price": 30.0, "sale_price": None, "in_stock": None},
    }
    now = [
        {"url": "u/drop", "competitor": "c", "name": "Drop", "price": 100.0, "sale_price": 75.0, "in_stock": True},
        {"url": "u/oos", "competitor": "c", "name": "Oos", "price": 50.0, "sale_price": None, "in_stock": False},
        {"url": "u/restock", "competitor": "c", "name": "Restock", "price": 50.0, "sale_price": None, "in_stock": True},
        {"url": "u/rise", "competitor": "c", "name": "Rise", "price": 25.0, "sale_price": None, "in_stock": True},
        {"url": "u/unknown", "competitor": "c", "name": "Unk", "price": 30.0, "sale_price": None, "in_stock": True},
        {"url": "u/new", "competitor": "c", "name": "New", "price": 5.0, "sale_price": None, "in_stock": True},
    ]
    alerts = find_changes(now, prev)
    check("three alerts", len(alerts), 3)
    check("kinds", sorted(a["kind"] for a in alerts),
          ["back_in_stock", "out_of_stock", "price_drop"])
    drop = next(a for a in alerts if a["kind"] == "price_drop")
    check("drop percentage", round(drop["pct"], 1), -25.0)
    check("price rise not alerted", any(a["product"] == "Rise" for a in alerts), False)
    check("unknown to known not alerted", any(a["product"] == "Unk" for a in alerts), False)
    check("unseen url not alerted", any(a["product"] == "New" for a in alerts), False)


def main():
    for test in (test_prices, test_availability, test_json_ld,
                 test_nested_text, test_changes):
        test()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
