"""
Competitor price and stock monitor.

Run it with no arguments for a normal daily run:

    python monitor.py

Useful flags while setting up:

    --check        verify configuration without touching the network
    --dry-run      scrape and print, but do not write to Sheets or send mail
    --test-email   send one test message and exit
    --no-email     scrape and write to Sheets, but stay quiet
    --limit N      only process the first N URLs
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import config
from extractor import extract

LOG = logging.getLogger("monitor")


def setup_logging():
    logfile = config.LOG_DIR / f"{datetime.now():%Y-%m}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.FileHandler(logfile, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )


# ----------------------------------------------------------------- inputs

def read_urls():
    """Read competitors.txt, ignoring blank lines and # comments."""
    if not config.COMPETITORS_FILE.exists():
        return []
    urls = []
    for line in config.COMPETITORS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith(("http://", "https://")):
            line = "https://" + line
        urls.append(line)
    return urls


def read_selectors():
    if not config.SELECTORS_FILE.exists():
        return {}
    try:
        return json.loads(config.SELECTORS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        LOG.warning("selectors.json is not valid JSON (%s); ignoring it", exc)
        return {}


def competitor_of(url):
    return urlparse(url).netloc.lower().removeprefix("www.")


# ---------------------------------------------------------------- scraping

def fetch_page(fetcher, url, proxy):
    """Fetch one URL with retries. Returns the page or raises the last error."""
    options = dict(
        headless=True,
        network_idle=True,
        timeout=config.PAGE_TIMEOUT,
        disable_resources=config.DISABLE_RESOURCES,
        block_webrtc=True,      # stop the real IP leaking past the proxy
        dns_over_https=bool(proxy),   # and stop DNS leaking either
        block_ads=True,
        google_search=True,
        solve_cloudflare=config.SOLVE_CLOUDFLARE,
        selector_config={
            "adaptive": True,
            "adaptive_domain": competitor_of(url),
            "storage_args": {"storage_file": str(config.ADAPTIVE_STORAGE)},
        },
    )
    if proxy:
        options["proxy"] = proxy

    last = None
    for attempt in range(1, config.MAX_RETRIES + 2):
        try:
            page = fetcher.fetch(url, **options)
            if page.status and page.status >= 400:
                raise RuntimeError(f"HTTP {page.status}")
            return page
        except Exception as exc:
            last = exc
            if attempt <= config.MAX_RETRIES:
                wait = attempt * 5
                LOG.warning("  attempt %d failed (%s); retrying in %ds",
                            attempt, exc, wait)
                time.sleep(wait)
    raise last


def scrape_all(urls, selectors, proxy):
    from scrapling.fetchers import StealthyFetcher

    StealthyFetcher.adaptive = True     # adaptive selector matching, globally

    results, failures = [], []
    for index, url in enumerate(urls, 1):
        LOG.info("[%d/%d] %s", index, len(urls), url)
        try:
            page = fetch_page(StealthyFetcher, url, proxy)
            product = extract(page, url, selectors)
        except Exception as exc:
            LOG.error("  failed: %s", exc)
            failures.append({"url": url, "error": f"{type(exc).__name__}: {exc}"})
            continue

        if product.error:
            LOG.warning("  %s", product.error)
            failures.append({"url": url, "error": product.error})
            continue

        record = product.as_dict()
        record["url"] = url
        record["competitor"] = competitor_of(url)
        results.append(record)
        LOG.info("  %s | price=%s sale=%s stock=%s (via %s)",
                 (product.name or "?")[:48], product.price,
                 product.sale_price, product.in_stock, product.source)

        if index < len(urls):
            time.sleep(config.DELAY_BETWEEN_URLS)

    return results, failures


# -------------------------------------------------------------- comparison

def effective_price(record):
    """What a shopper would actually pay: the sale price if there is one."""
    sale = record.get("sale_price")
    return sale if sale is not None else record.get("price")


def load_snapshot():
    if not config.SNAPSHOT_FILE.exists():
        return {}
    try:
        return json.loads(config.SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        LOG.warning("previous snapshot was unreadable; treating as first run")
        return {}


def save_snapshot(results):
    snapshot = {r["url"]: r for r in results}
    config.SNAPSHOT_FILE.write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def find_changes(results, previous):
    """Compare this run to the last one and return a list of alert dicts."""
    alerts = []
    for record in results:
        before = previous.get(record["url"])
        if not before:
            continue                      # first time we have seen this URL

        now_price = effective_price(record)
        was_price = effective_price(before)
        if now_price is not None and was_price is not None and now_price < was_price:
            alerts.append({
                "kind": "price_drop",
                "competitor": record["competitor"],
                "product": record.get("name") or "(unnamed)",
                "was": was_price,
                "now": now_price,
                "pct": (now_price - was_price) / was_price * 100,
                "currency": record.get("currency"),
                "url": record["url"],
            })

        now_stock, was_stock = record.get("in_stock"), before.get("in_stock")
        if was_stock is True and now_stock is False:
            alerts.append({
                "kind": "out_of_stock",
                "competitor": record["competitor"],
                "product": record.get("name") or "(unnamed)",
                "was": "in stock", "now": "out of stock",
                "url": record["url"],
            })
        elif was_stock is False and now_stock is True:
            alerts.append({
                "kind": "back_in_stock",
                "competitor": record["competitor"],
                "product": record.get("name") or "(unnamed)",
                "was": "out of stock", "now": "in stock",
                "url": record["url"],
            })
    return alerts


# ------------------------------------------------------------------- rows

def stock_cell(value):
    return {True: "yes", False: "no"}.get(value, "unknown")


def results_rows(results, stamp):
    return [[
        stamp,
        r["competitor"],
        r.get("name") or "",
        r.get("price") if r.get("price") is not None else "",
        r.get("sale_price") if r.get("sale_price") is not None else "",
        stock_cell(r.get("in_stock")),
        r["url"],
    ] for r in results]


def alerts_rows(alerts, stamp):
    labels = {
        "price_drop": "price drop",
        "out_of_stock": "went out of stock",
        "back_in_stock": "back in stock",
    }
    rows = []
    for a in alerts:
        was, now = a["was"], a["now"]
        if a["kind"] == "price_drop":
            was, now = f"{was:.2f}", f"{now:.2f}"
        rows.append([stamp, a["competitor"], a["product"],
                     labels[a["kind"]], was, now, a["url"]])
    return rows


# ----------------------------------------------------------- proxy check

def test_proxy():
    """
    Prove the proxy is actually carrying the traffic.

    Fetches an address-echo service twice, once directly and once through the
    proxy, and compares. A configured proxy that quietly fails open would
    otherwise look identical to a working one, while exposing your real
    address to every site you monitor.
    """
    import json as _json
    from scrapling.fetchers import StealthyFetcher

    proxy = config.proxy_config()
    if not proxy:
        LOG.error("no proxy configured: PROXY_SERVER is empty in .env")
        return 1

    LOG.info("proxy server   : %s", proxy["server"])
    LOG.info("proxy username : %s", proxy.get("username") or "(none)")
    LOG.info("proxy password : %s", "set" if proxy.get("password") else "(none)")

    echo = "https://api.ipify.org?format=json"

    def address(use_proxy):
        options = dict(headless=True, timeout=45000, block_webrtc=True)
        if use_proxy:
            options["proxy"] = proxy
            options["dns_over_https"] = True
        page = StealthyFetcher.fetch(echo, **options)
        try:
            return _json.loads(page.get_all_text().strip())["ip"]
        except Exception:
            return (page.get_all_text() or "").strip()[:60]

    try:
        direct = address(False)
        LOG.info("address without proxy : %s", direct)
    except Exception as exc:
        direct = None
        LOG.warning("direct check failed (%s); continuing", exc)

    try:
        through = address(True)
    except Exception as exc:
        LOG.error("could not reach the internet through the proxy: %s", exc)
        LOG.error("check the server, port, username and password in .env")
        return 1

    LOG.info("address through proxy : %s", through)

    if direct and through == direct:
        LOG.error("PROXY NOT WORKING: both addresses are the same, so traffic "
                  "is not going through the proxy")
        return 1

    LOG.info("proxy is working: sites will see %s", through)
    return 0


# ------------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description="Competitor price and stock monitor")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-email", action="store_true")
    parser.add_argument("--test-proxy", action="store_true")
    parser.add_argument("--no-email", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    setup_logging()

    if args.test_proxy:
        return test_proxy()

    if args.test_email:
        import notify
        ok, message = notify.send(
            "Competitor Intel: test message",
            "If you are reading this, email alerts are working.",
        )
        LOG.info("test email: %s", message)
        return 0 if ok else 1

    problems = config.missing_requirements()
    if args.check:
        LOG.info("sheet name     : %s", config.SHEET_NAME)
        LOG.info("secrets folder : %s", config.SECRETS_DIR)
        LOG.info("proxy          : %s",
                 "configured" if config.proxy_config() else "not set (direct connection)")
        LOG.info("URLs           : %d", len(read_urls()))
        if problems:
            for p in problems:
                LOG.error("  missing: %s", p)
            return 1
        LOG.info("configuration looks complete")
        return 0

    urls = read_urls()
    if not urls:
        LOG.error("no URLs in %s", config.COMPETITORS_FILE)
        return 1
    if args.limit:
        urls = urls[: args.limit]

    proxy = config.proxy_config()
    LOG.info("starting run: %d URL(s), proxy %s",
             len(urls), "on" if proxy else "off")

    results, failures = scrape_all(urls, read_selectors(), proxy)
    LOG.info("scraped %d ok, %d failed", len(results), len(failures))

    previous = load_snapshot()
    alerts = find_changes(results, previous) if previous else []
    if not previous:
        LOG.info("first run: nothing to compare against yet")
    LOG.info("%d alert(s)", len(alerts))

    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")

    if args.dry_run:
        LOG.info("dry run: not writing to Sheets or sending email")
        for row in results_rows(results, stamp):
            LOG.info("  ROW %s", row)
        for row in alerts_rows(alerts, stamp):
            LOG.info("  ALERT %s", row)
        return 0

    if problems:
        for p in problems:
            LOG.error("cannot finish: %s", p)
        return 1

    import sheets
    try:
        _, results_tab, alerts_tab = sheets.open_workbook()
        sheets.append_rows(results_tab, results_rows(results, stamp))
        sheets.append_rows(alerts_tab, alerts_rows(alerts, stamp))
        LOG.info("wrote %d row(s) to '%s'", len(results), config.SHEET_NAME)
    except RuntimeError as exc:
        # Setup problems carry their own instructions; print them as written
        # rather than squashing a multi-line explanation onto one log line.
        LOG.error("Google Sheets is not ready yet.")
        for line in str(exc).splitlines():
            print(line)
        return 1
    except Exception as exc:
        LOG.error("Google Sheets write failed: %s: %s", type(exc).__name__, exc)
        return 1

    save_snapshot(results)

    if not args.no_email and (alerts or failures):
        import notify
        subject, text, html = notify.build_summary(alerts, len(results), failures)
        ok, message = notify.send(subject, text, html)
        LOG.info("email: %s", message)
    elif not args.no_email:
        LOG.info("no alerts, so no email sent")

    return 0


if __name__ == "__main__":
    sys.exit(main())
