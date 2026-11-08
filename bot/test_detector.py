import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from bot.browser import Monitor
from bot.detector import (
    check_budget, check_cart_count, check_shopping_visits,
    detect_ad_referral, is_shopping_site,
)


class DetectorTests(unittest.TestCase):
    def test_domains(self):
        for url in ("https://amazon.com", "https://www.target.com/cart", "https://shop.walmart.com"):
            self.assertTrue(is_shopping_site(url))
        for url in ("about:blank", "https://fakeamazon.com", "https://amazon.com.evil.com", "https://amazon.com@evil.com"):
            self.assertFalse(is_shopping_site(url))

    def test_referrals(self):
        for key in ("gclid", "fbclid", "msclkid"):
            self.assertTrue(detect_ad_referral("https://target.com/?" + key + "="))
        self.assertFalse(detect_ad_referral("https://target.com/#gclid=123"))

    def test_thresholds(self):
        self.assertFalse(check_budget(100, 100))
        self.assertTrue(check_budget(100.01, 100))
        self.assertFalse(check_cart_count(5))
        self.assertTrue(check_cart_count(6))
        self.assertFalse(check_shopping_visits(6))
        self.assertTrue(check_shopping_visits(7))

    @patch("bot.browser.read_cart", return_value={"total": 151, "count": 6})
    def test_navigation_deduplication_and_reset(self, read_cart):
        monitor = Monitor(150)
        monitor.alert = Mock(return_value=True)
        page = Mock(url="https://amazon.com/cart?gclid=123")
        monitor.poll(page)
        monitor.poll(page)
        self.assertEqual([call.args[0] for call in monitor.alert.call_args_list],
                         ["shopping_visit", "ad_referral", "over_budget", "cart_count"])
        read_cart.return_value = {"total": 100, "count": 2}
        monitor.poll(page)
        read_cart.return_value = {"total": 151, "count": 6}
        monitor.poll(page)
        self.assertEqual(monitor.alert.call_count, 6)

    @patch("bot.browser.read_cart", return_value={"total": None, "count": None})
    def test_visits_expire_and_seventh_visit_warns(self, read_cart):
        monitor = Monitor(150)
        monitor.alert = Mock(return_value=True)
        monitor.visits.append(datetime.now(timezone.utc) - timedelta(days=8))
        page = Mock()
        for number in range(7):
            page.url = f"https://target.com/p/{number}"
            monitor.poll(page)
        self.assertEqual(len(monitor.visits), 7)
        self.assertEqual(monitor.alert.call_args.args, ("shopping_visits", page.url, 7))


if __name__ == "__main__":
    unittest.main()
