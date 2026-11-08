"""Run with python -m bot.browser --weekly-budget 150."""

import argparse
import logging
import math
import re
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

from .detector import (
    check_budget, check_cart_count, check_shopping_visits,
    detect_ad_referral, is_shopping_site,
)

API_URL = "http://127.0.0.1:8000/alert"

# Retailer markup changes. Read only labeled cart values, never arbitrary prices.
CART_SELECTORS = {
    "amazon.com": {
        "total": ['#sc-subtotal-amount-activecart'],
        "count": ['#sc-subtotal-label-activecart'],
    },
    "target.com": {
        "total": ['[data-test="cart-summary-subTotal"]'],
        "count": ['[data-test="cart-items-count"]'],
    },
    "walmart.com": {
        "total": ['[data-testid="subtotal"]'],
        "count": ['[data-testid="cart-item-count"]'],
    },
}


def send_alert(alert_type, website, value=None, api_url=API_URL):
    try:
        response = requests.post(
            api_url, json={"type": alert_type, "website": website, "value": value}, timeout=3,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        logging.warning("Could not deliver %s: %s", alert_type, exc)
        return False


def read_cart(page):
    host = (urlparse(page.url).hostname or "").lower().rstrip(".")
    selectors = next((values for site, values in CART_SELECTORS.items()
                      if host == site or host.endswith("." + site)), {})
    result = {"total": None, "count": None}
    for field, candidates in selectors.items():
        for selector in candidates:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible():
                text = locator.inner_text(timeout=500)
                pattern = r"\$\s*([0-9][0-9,]*(?:\.[0-9]{2})?)" if field == "total" else r"\b([0-9][0-9,]*)\b"
                match = re.search(pattern, text)
                if match:
                    value = match.group(1).replace(",", "")
                    result[field] = float(value) if field == "total" else int(value)
                    break
    return result


class Monitor:
    def __init__(self, weekly_budget, api_url=API_URL, dry_run=False):
        self.weekly_budget = weekly_budget
        self.api_url = api_url
        self.dry_run = dry_run
        self.visits = deque()
        self.previous_urls = {}
        self.active_rules = {}

    def alert(self, kind, url, value=None):
        if self.dry_run:
            print(f"ALERT {kind:<16} value={value}  {url}", flush=True)
            return True
        return send_alert(kind, url, value, self.api_url)

    def poll(self, page):
        url = page.url
        previous = self.previous_urls.get(page)
        self.previous_urls[page] = url
        if not is_shopping_site(url):
            self.active_rules.pop(page, None)
            return
        now = datetime.now(timezone.utc)
        while self.visits and self.visits[0] <= now - timedelta(days=7):
            self.visits.popleft()
        if url != previous:
            logging.info("Visited: %s", url)
            self.visits.append(now)
            self.alert("shopping_visit", url, len(self.visits))
            if detect_ad_referral(url):
                self.alert("ad_referral", url)
        cart = read_cart(page)
        rules = {
            "over_budget": (cart["total"] is not None and check_budget(cart["total"], self.weekly_budget), cart["total"]),
            "cart_count": (cart["count"] is not None and check_cart_count(cart["count"]), cart["count"]),
            "shopping_visits": (check_shopping_visits(len(self.visits)), len(self.visits)),
        }
        active = self.active_rules.setdefault(page, set())
        for kind, (triggered, value) in rules.items():
            if triggered and kind not in active:
                if self.alert(kind, url, value):
                    active.add(kind)
            elif not triggered:
                active.discard(kind)


def main():
    parser = argparse.ArgumentParser(description="Open a browser monitored by Piggyback.")
    parser.add_argument("--weekly-budget", type=float, required=True, help="Cart limit in USD")
    parser.add_argument("--api-url", default=API_URL)
    parser.add_argument("--start-url", default="https://www.google.com")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print alerts instead of sending them to the brain")
    parser.add_argument("--chrome", action="store_true",
                        help="Use installed Google Chrome instead of Playwright's Chromium")
    args = parser.parse_args()
    if not math.isfinite(args.weekly_budget) or args.weekly_budget < 0:
        parser.error("--weekly-budget must be a finite, nonnegative number")
    from playwright.sync_api import Error, sync_playwright

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    monitor = Monitor(args.weekly_budget, args.api_url, args.dry_run)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="chrome" if args.chrome else None)
        context = browser.new_context()
        try:
            page = context.new_page()
            page.goto(args.start_url, wait_until="domcontentloaded")
            while browser.is_connected() and context.pages:
                for page in list(context.pages):
                    try:
                        monitor.poll(page)
                    except Error as exc:
                        logging.debug("Page changed while reading: %s", exc)
                for closed in set(monitor.previous_urls) - set(context.pages):
                    monitor.previous_urls.pop(closed, None)
                    monitor.active_rules.pop(closed, None)
                if context.pages:
                    context.pages[0].wait_for_timeout(1000)
        except KeyboardInterrupt:
            pass
        except Error:
            if browser.is_connected() and context.pages:
                raise
        finally:
            browser.close()


if __name__ == "__main__":
    main()
