import unittest

from ingest.crawl.scope import canonicalize, in_scope, normalize_base, should_skip_path


class ScopeTests(unittest.TestCase):
    def test_locale_home_strips_index_and_keeps_prefix(self):
        normalized, host, prefix = normalize_base("https://www2.hm.com/en_us/index.html")
        self.assertEqual(normalized, "https://www2.hm.com/en_us")
        self.assertEqual(host, "www2.hm.com")
        self.assertEqual(prefix, "/en_us")

    def test_narrow_base_stays_under_that_path(self):
        _, host, prefix = normalize_base("https://www.zara.com/us/en/")
        self.assertEqual(host, "www.zara.com")
        self.assertEqual(prefix, "/us/en")
        self.assertTrue(in_scope("https://www.zara.com/us/en/woman", host, prefix))
        self.assertFalse(in_scope("https://www.zara.com/es/en/woman", host, prefix))

    def test_tracking_params_are_removed_and_facets_are_dropped(self):
        url = canonicalize(
            "/en_us/product/1?utm_source=x&color=red&size=m&sort=price",
            "https://shop.example/en_us",
        )
        self.assertIsNone(url)
        kept = canonicalize("/en_us/product/1?color=red&utm_medium=email", "https://shop.example/en_us")
        self.assertEqual(kept, "https://shop.example/en_us/product/1?color=red")

    def test_account_and_files_are_skipped(self):
        self.assertTrue(should_skip_path("https://shop.example/en_us/account/orders"))
        self.assertIsNone(canonicalize("/en_us/lookbook.pdf", "https://shop.example/en_us"))

    def test_adds_https_when_missing(self):
        normalized, host, prefix = normalize_base("www2.hm.com/en_us")
        self.assertEqual(normalized, "https://www2.hm.com/en_us")
        self.assertEqual(host, "www2.hm.com")
        self.assertEqual(prefix, "/en_us")


if __name__ == "__main__":
    unittest.main()
