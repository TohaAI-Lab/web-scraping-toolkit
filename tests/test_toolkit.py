import csv
import json
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from scraping.core import Client, RobotsPolicy, ScrapeError, canonical, MAX_BYTES
from soupsieve import SelectorSyntaxError
from scraping.extract import collect, extract, parse_fields
from scraping.export import export
from scraper import main

ROOT = Path(__file__).resolve().parents[1]
FIELDS = parse_fields(["name=h2", "price=.price", "link=a::attr(href)"])


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class ExtractionTests(unittest.TestCase):
    def test_extract_text_and_attributes(self):
        rows, link = extract((ROOT / "examples/index.html").read_text(), ".product", FIELDS, ".next")
        self.assertEqual(rows[0], {"name": "Keyboard", "price": "49.00", "link": "/keyboard"})
        self.assertEqual(link, "page2.html")

    def test_missing_field(self):
        rows, _ = extract('<div class="product"><h2>Test</h2></div>', ".product", FIELDS)
        self.assertEqual(rows, [{"name": "Test", "price": "", "link": ""}])

    def test_reject_duplicate_fields(self):
        with self.assertRaises(ValueError):
            parse_fields(["name=h2", "name=h3"])

    def test_invalid_css(self):
        with self.assertRaises(SelectorSyntaxError):
            parse_fields(["name=["])

    def test_pagination_and_dedup(self):
        client = Mock()
        client.fetch.side_effect = [(ROOT / "examples" / f).read_text() for f in ("index.html", "page2.html")]
        rows = collect(client, "https://example.com/index.html", ".product", FIELDS, ".next", 2)
        self.assertEqual([r["name"] for r in rows], ["Keyboard", "Mouse", "Monitor"])
        self.assertEqual(client.fetch.call_count, 2)

    def test_pagination_cycle(self):
        client = Mock()
        client.fetch.return_value = '<a class="next" href="/#x">Next</a>'
        self.assertEqual(collect(client, "https://example.com/", ".product", FIELDS, ".next", 10), [])
        self.assertEqual(client.fetch.call_count, 1)

    def test_cross_origin_blocked(self):
        client = Mock()
        client.fetch.return_value = '<a class="next" href="https://other.example/">Next</a>'
        with self.assertRaises(ScrapeError):
            collect(client, "https://example.com/", ".product", FIELDS, ".next", 2)
        self.assertEqual(client.fetch.call_count, 1)

    def test_max_pages(self):
        client = Mock()
        client.fetch.return_value = '<a class="next" href="/other">Next</a>'
        collect(client, "https://example.com/", ".product", FIELDS, ".next", 1)
        self.assertEqual(client.fetch.call_count, 1)


class PolicyTests(unittest.TestCase):
    def test_specific_agent_and_allow(self):
        policy = RobotsPolicy("User-agent: *\nDisallow: /\nUser-agent: TohaAIScraper\nDisallow: /private\nAllow: /private/public\nCrawl-delay: 2")
        self.assertTrue(policy.allows("https://example.com/"))
        self.assertFalse(policy.allows("https://example.com/private/a"))
        self.assertTrue(policy.allows("https://example.com/private/public"))
        self.assertEqual(policy.delay, 2)

    def test_wildcard_and_end(self):
        policy = RobotsPolicy("User-agent: *\nDisallow: /*.pdf$\n")
        self.assertFalse(policy.allows("https://example.com/file.pdf"))
        self.assertTrue(policy.allows("https://example.com/file.pdf?q=1"))

    def test_ambiguous_encoding_denied(self):
        policy = RobotsPolicy("User-agent: *\nDisallow: /private")
        self.assertFalse(policy.allows("https://example.com/%70rivate"))
        self.assertFalse(policy.allows("https://example.com/a/../private"))
        with self.assertRaises(ScrapeError):
            RobotsPolicy("User-agent: *\nDisallow: /%70rivate")

    def test_unsupported_request_rate(self):
        with self.assertRaises(ScrapeError):
            RobotsPolicy("User-agent: *\nRequest-rate: 1/10")

    def test_invalid_urls(self):
        for url in ("file:///etc/passwd", "https://user:password@example.com", "https://example.com:99999", "https://example.com/a b"):
            with self.subTest(url=url), self.assertRaises(ScrapeError):
                canonical(url)
        self.assertEqual(canonical("https://EXAMPLE.com:443/#hello"), "https://example.com/")

    def test_retry_after(self):
        self.assertEqual(Client.retry_wait("10", 0), 10)
        with self.assertRaises(ScrapeError):
            Client.retry_wait("120", 0)

    def test_denied_url_never_requested(self):
        with Client("https://example.com/") as client, patch.object(client, "_get", return_value="User-agent: *\nDisallow: /private") as get:
            with self.assertRaises(ScrapeError):
                client.fetch("https://example.com/private")
            get.assert_called_once_with("https://example.com/robots.txt", robots=True)

    def test_robots_failure_blocks_page(self):
        with Client("https://example.com/") as client, patch.object(client, "_get", side_effect=ScrapeError("HTTP 403")) as get:
            with self.assertRaises(ScrapeError):
                client.fetch(client.start_url)
            self.assertEqual(get.call_count, 1)

    def test_retry_503_then_success(self):
        bad, good = Mock(), Mock()
        bad.status_code, bad.headers = 503, {}
        good.status_code, good.headers, good.encoding = 200, {"Content-Type": "text/html"}, "utf-8"
        good.iter_content.return_value = [b"<p>ok</p>"]
        from unittest.mock import MagicMock
        a, b = MagicMock(), MagicMock()
        a.__enter__.return_value, b.__enter__.return_value = bad, good
        with Client("https://example.com/") as client, patch.object(client.session, "get", side_effect=[a,b]) as get, patch("scraping.core.time.sleep"):
            self.assertEqual(client._get(client.start_url), "<p>ok</p>")
            self.assertEqual(get.call_count, 2)

    def test_access_denied_not_retried(self):
        from unittest.mock import MagicMock
        reply = MagicMock()
        reply.__enter__.return_value.status_code = 403
        with Client("https://example.com/") as client, patch.object(client.session, "get", return_value=reply) as get:
            with self.assertRaises(ScrapeError):
                client._get(client.start_url)
            self.assertEqual(get.call_count, 1)

    def test_response_size_limit(self):
        from unittest.mock import MagicMock
        reply = MagicMock()
        response = reply.__enter__.return_value
        response.status_code = 200
        response.headers = {"Content-Type": "text/html"}
        response.iter_content.return_value = [b"x" * (MAX_BYTES + 1)]
        with Client("https://example.com/") as client, patch.object(client.session, "get", return_value=reply):
            with self.assertRaisesRegex(ScrapeError, "2 MiB"):
                client._get(client.start_url)

    def test_utf8_without_charset(self):
        from unittest.mock import MagicMock
        reply = MagicMock()
        response = reply.__enter__.return_value
        response.status_code = 200
        response.headers = {"Content-Type": "text/html"}
        response.encoding = "ISO-8859-1"
        response.iter_content.return_value = ["<p>Київ</p>".encode()]
        with Client("https://example.com/") as client, patch.object(client.session, "get", return_value=reply):
            self.assertEqual(client._get(client.start_url), "<p>Київ</p>")

    def test_redirect_not_followed(self):
        from unittest.mock import MagicMock
        reply = MagicMock()
        reply.__enter__.return_value.status_code = 302
        with Client("https://example.com/") as client, patch.object(client.session, "get", return_value=reply) as get:
            with self.assertRaises(ScrapeError):
                client._get(client.start_url)
            self.assertFalse(get.call_args.kwargs["allow_redirects"])
            self.assertEqual(get.call_count, 1)


class ExportTests(unittest.TestCase):
    def test_json_unicode(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.json"
            export([{"name": "Київ"}], ["name"], p, "json")
            self.assertEqual(json.loads(p.read_text(encoding="utf-8")), [{"name": "Київ"}])

    def test_csv_formula_mitigation(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.csv"
            export([{"name": " =1+1"}, {"name": "normal"}], ["name"], p, "csv")
            with p.open(newline="", encoding="utf-8") as f:
                self.assertEqual(list(csv.DictReader(f)), [{"name": "' =1+1"}, {"name": "normal"}])

    def test_empty_csv_header(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.csv"
            export([], ["name", "price"], p, "csv")
            self.assertEqual(p.read_text().strip(), "name,price")

    def test_local_http_end_to_end(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(ROOT / "examples")))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "results.json"
                url = f"http://127.0.0.1:{server.server_port}/index.html"
                code = main([url, "--item", ".product", "--field", "name=h2", "--field", "price=.price", "--next", ".next", "--max-pages", "2", "--output", str(p)])
                self.assertEqual(code, 0)
                self.assertEqual([r["name"] for r in json.loads(p.read_text())], ["Keyboard", "Mouse", "Monitor"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

if __name__ == "__main__":
    unittest.main()
