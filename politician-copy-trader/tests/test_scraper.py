#!/usr/bin/env python3
"""Run from politician-copy-trader: python tests/test_scraper.py"""
import os, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scraper

PAGE = """<html><h1>{name}</h1><table><thead><tr><th>Traded Issuer</th></tr></thead><tbody>
<tr><td>Warner Bros Discover Inc WBD:US</td><td>9 Nov 2026</td><td>7 Oct 2026</td><td>days 30</td><td>sell</td><td>50K–100K</td><td>link</td></tr>
<tr><td>REOF XXX LLC N/A</td><td>5 Oct 2026</td><td>8 Sept 2026</td><td>days 24</td><td>buy</td><td>500K–1M</td><td>link</td></tr>
<tr><td>Broken Co BRK:US</td><td>5 Oct 2026</td><td>not a date</td><td>days 1</td><td>buy</td><td>1K–15K</td><td>link</td></tr>
</tbody></table></html>"""


def fake_get(name):
    resp = mock.Mock(status_code=200, text=PAGE.format(name=name))
    resp.raise_for_status = lambda: None
    return resp


class TestScraper(unittest.TestCase):
    def test_parses_dates_type_and_size(self):
        with mock.patch.object(scraper.requests, "get", return_value=fake_get("Nancy Pelosi")):
            trades = scraper.fetch_trades("P000197", expected_name="Nancy Pelosi")
        self.assertEqual([t.unique_key for t in trades],
                         ["WBD:US|2026-10-07|sell|50K-100K", "N/A|2026-09-08|buy|500K-1M"])  # 'Sept' parses; bad date dropped

    def test_wrong_person_is_refused(self):
        # 2026-11-20: Capitol Trades served another legislator's feed for every ID
        with mock.patch.object(scraper.requests, "get", return_value=fake_get("Thomas Kean Jr")):
            with self.assertRaises(ValueError):
                scraper.fetch_trades("P000197", expected_name="Nancy Pelosi")


if __name__ == "__main__":
    unittest.main()
