# Catalog ingest

Give this service a store base URL. It walks that site in the order the navigation is written (Women, Men, Kids, and the sections under them) and saves products to Postgres. Price, SKU, images, variants, and the category path are kept when the page exposes them.

Pass the locale home if you want that whole locale, for example `https://www2.hm.com/en_us`. The crawl stays on that host and under that path, so a US link does not wander into other countries. A narrower URL ingests only that section.

Coverage depends on what the site renders. The crawler reads the page, schema.org data, and JSON the page itself loads. It does not bypass CAPTCHAs, login walls, or bot checks. A blocked URL is recorded and skipped. `robots.txt` is followed. The default pace is about one page per second, with a cap of 2000 pages.

## What is stored

Postgres tables:

- `sites` and `crawl_runs`
- `categories` in parent/child order, with a path such as `Women / Dresses`
- `products` (name, description, brand, SKU, price, compare-at price, currency, availability, URL, category, raw snapshot)
- `product_variants` (SKU, color, size, price, availability)
- `product_images` (source URL, and a blob URL when upload is configured)

Image files are uploaded to Azure Blob only when both `AZURE_STORAGE_CONNECTION_STRING` and `AZURE_BLOB_CONTAINER` are set. Otherwise the image URL is stored and the file is left on the store's CDN.

## Run with Docker

On a laptop or an Azure Ubuntu VM:

```bash
cp .env.example .env
# optional: set AZURE_STORAGE_CONNECTION_STRING in .env
docker compose up -d --build
```

The API listens on port 8080. Postgres is bound to `127.0.0.1:5432` on the machine so it is not published to the internet. In the Azure network security group, open port 8080 only if you need to call the API from outside the VM.

Start a crawl from the API:

```bash
curl -s -X POST http://127.0.0.1:8080/v1/crawls \
  -H 'content-type: application/json' \
  -d '{"url":"https://www2.hm.com/en_us","max_pages":200}'
```

Or run the same crawl in a one-off container:

```bash
docker compose run --rm ingest python -m ingest crawl \
  --url https://www2.hm.com/en_us \
  --max-pages 200
```

Check progress:

```bash
curl -s http://127.0.0.1:8080/v1/crawls/1
curl -s http://127.0.0.1:8080/v1/sites/www2.hm.com/categories
curl -s 'http://127.0.0.1:8080/v1/sites/www2.hm.com/products?category=Women&limit=50'
```

`category` is the stored path (`Women` or `Women / Dresses`). `category_id` works too. Add `prefix` when the same host has more than one locale.

The first image build installs Chromium and is large. Later starts reuse it. `restart: unless-stopped` brings the API and Postgres back after a VM reboot.

## Run without Docker

Python 3.12, then:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
docker compose up -d postgres
cp .env.example .env
export PYTHONPATH=src
python -m ingest crawl --url https://www2.hm.com/en_us
python -m ingest serve --host 0.0.0.0 --port 8080
```

## Configuration

| Variable | Default | Role |
| --- | --- | --- |
| `DATABASE_URL` | local Postgres URL | Catalog database |
| `AZURE_STORAGE_CONNECTION_STRING` | empty | Enables image upload |
| `AZURE_BLOB_CONTAINER` | `catalog-images` | Blob container |
| `CRAWL_DELAY_SECONDS` | `1` | Pause between page loads. A larger `Crawl-delay` in robots.txt wins |
| `MAX_PAGES` | `2000` | Default page cap. Raise it for a larger catalog |
| `MAX_DEPTH` | `8` | How deep category links are followed |
| `MAX_IMAGE_BYTES` | `8000000` | Skip larger image files |
| `MAX_IMAGES_PER_PRODUCT` | `8` | Images saved per product |

A full H&M or Zara catalog is tens of thousands of pages. Raise `--max-pages` when you want more than the default. Pages that never show a price or SKU are stored with those fields empty. Blocked pages are not retried through proxies.

Only one crawl runs at a time through the API. Run the server as a single process.
# data-ingest-hm
