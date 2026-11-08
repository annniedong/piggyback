"""Pure detection rules; thresholds are strictly greater than the limits."""

from urllib.parse import parse_qs, urlparse

SHOPPING_SITES = {"amazon.com", "target.com", "walmart.com", "urbanoutfitters.com"}


def is_shopping_site(url):
    hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(hostname == site or hostname.endswith("." + site) for site in SHOPPING_SITES)


def detect_ad_referral(url):
    params = parse_qs(urlparse(url).query, keep_blank_values=True)
    return any(key in params for key in ("gclid", "fbclid", "msclkid"))


def check_budget(cart_total, weekly_budget):
    return cart_total > weekly_budget


def check_cart_count(item_count):
    return item_count > 5


def check_shopping_visits(visit_count):
    return visit_count > 6
