import unittest

from ingest.crawl.extract import extract_page, parse_decimal, parse_price_value

HOST = "shop.example"
PREFIX = "/en_us"
HOME = "https://shop.example/en_us"

LISTING = """
<html>
<head>
  <title>Dresses | Example</title>
  <script type="application/ld+json">
  {"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[
    {"@type":"ListItem","position":1,"name":"Women","item":"https://shop.example/en_us/women"},
    {"@type":"ListItem","position":2,"name":"Dresses","item":"https://shop.example/en_us/women/dresses"}
  ]}
  </script>
  <script type="application/ld+json">
  {"@context":"https://schema.org","@type":"ItemList","itemListElement":[
    {"@type":"ListItem","position":1,"url":"https://shop.example/en_us/product/123","name":"Red Dress"}
  ]}
  </script>
</head>
<body>
  <header>
    <nav>
      <a href="/en_us/women">Women</a>
      <a href="/en_us/men">Men</a>
      <a href="/en_us/kids">Kids</a>
      <a href="/en_gb/women">UK women</a>
      <a href="/en_us/account">Account</a>
    </nav>
  </header>
  <main>
    <a href="/en_us/women/dresses?page=2">Next</a>
    <article>
      <a href="/en_us/product/123">Red Dress</a>
      <span>$49.00</span>
      <img src="https://cdn.example/red.jpg" alt="Red Dress" />
    </article>
  </main>
</body>
</html>
"""

PDP = """
<html>
<head>
  <title>Red Dress</title>
  <meta property="og:type" content="product" />
  <script type="application/ld+json">
  {
    "@type": "Product",
    "name": "Red Dress",
    "sku": "12345",
    "image": ["https://cdn.example/red.jpg", "https://cdn.example/red-2.jpg"],
    "brand": {"@type": "Brand", "name": "Example"},
    "description": "A red dress",
    "offers": [
      {"@type": "Offer", "sku": "12345-S", "price": "49.00", "priceCurrency": "USD", "size": "S",
       "availability": "https://schema.org/InStock"},
      {"@type": "Offer", "sku": "12345-M", "price": "49.00", "priceCurrency": "USD", "size": "M",
       "availability": "https://schema.org/OutOfStock"}
    ]
  }
  </script>
  <script type="application/ld+json">
  {"@type":"BreadcrumbList","itemListElement":[
    {"@type":"ListItem","position":1,"name":"Women","item":"https://shop.example/en_us/women"},
    {"@type":"ListItem","position":2,"name":"Red Dress","item":"https://shop.example/en_us/product/123"}
  ]}
  </script>
</head>
<body>
  <h1>Red Dress</h1>
</body>
</html>
"""


class ExtractTests(unittest.TestCase):
    def test_home_nav_keeps_order_and_stays_in_locale(self):
        page = extract_page(LISTING, HOME, [], host=HOST, prefix=PREFIX, is_home=True)
        names = [link.name for link in page.links if link.kind == "category"]
        self.assertEqual(names[:3], ["Women", "Men", "Kids"])
        self.assertNotIn("Account", names)
        self.assertNotIn("UK women", names)
        self.assertTrue(any(link.kind == "pagination" and link.url.endswith("page=2") for link in page.links))

    def test_listing_card_merges_price_image_and_itemlist_url(self):
        page = extract_page(
            LISTING,
            "https://shop.example/en_us/women/dresses",
            [],
            host=HOST,
            prefix=PREFIX,
            is_home=False,
        )
        self.assertFalse(page.is_product)
        self.assertEqual(page.breadcrumbs[0].name, "Women")
        self.assertEqual(page.breadcrumbs[1].name, "Dresses")
        product = next(item for item in page.products if item.url and item.url.endswith("/product/123"))
        self.assertEqual(product.name, "Red Dress")
        self.assertEqual(product.price, parse_decimal("49.00"))
        self.assertEqual(product.currency, "USD")
        self.assertEqual(product.images[0].url, "https://cdn.example/red.jpg")

    def test_product_page_has_sku_variants_and_price(self):
        page = extract_page(
            PDP,
            "https://shop.example/en_us/product/123",
            [],
            host=HOST,
            prefix=PREFIX,
            is_home=False,
        )
        self.assertTrue(page.is_product)
        main = next(product for product in page.products if product.is_main)
        self.assertEqual(main.sku, "12345")
        self.assertEqual(main.brand, "Example")
        self.assertEqual(main.price, parse_decimal("49.00"))
        self.assertEqual(main.currency, "USD")
        self.assertEqual(main.availability, "InStock")
        self.assertEqual([variant.size for variant in main.variants], ["S", "M"])
        self.assertEqual(len(main.images), 2)

    def test_json_payload_product(self):
        payload = {
            "products": [
                {
                    "name": "Blue Shirt",
                    "price": 29.5,
                    "currency": "EUR",
                    "image": "https://cdn.example/blue.jpg",
                    "sku": "B1",
                    "url": "https://shop.example/en_us/product/b1",
                    "sizes": ["S", "M"],
                }
            ]
        }
        page = extract_page("<html><body><main></main></body></html>", HOME, [payload], host=HOST, prefix=PREFIX, is_home=True)
        product = page.products[0]
        self.assertEqual(product.sku, "B1")
        self.assertEqual(product.currency, "EUR")
        self.assertEqual(str(product.price), "29.50")
        self.assertEqual([variant.size for variant in product.variants], ["S", "M"])

    def test_item_list_stays_a_category_page(self):
        html = """
        <html><head>
          <meta property="og:type" content="product" />
          <script type="application/ld+json">
          {"@type":"ItemList","itemListElement":[
            {"@type":"ListItem","url":"https://shop.example/en_us/product/1"},
            {"@type":"ListItem","url":"https://shop.example/en_us/product/2"},
            {"@type":"ListItem","url":"https://shop.example/en_us/product/3"}
          ]}
          </script>
        </head><body><main><h1>Women</h1></main></body></html>
        """
        page = extract_page(html, "https://shop.example/en_us/women", [], host=HOST, prefix=PREFIX, is_home=False)
        self.assertFalse(page.is_product)
        self.assertEqual(page.heading, "Women")
        urls = {product.url for product in page.products}
        self.assertEqual(len(urls), 3)

    def test_european_price(self):
        amount, _ = parse_price_value("1.299,00 EUR")
        self.assertEqual(str(amount), "1299.00")


if __name__ == "__main__":
    unittest.main()
