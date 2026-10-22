import argparse
import logging
import sys

from ingest.config import get_settings
from ingest.db.models import CrawlRun
from ingest.db.session import session_scope
from ingest.pipeline import execute_crawl, start_crawl


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ingest", description="Ingest a public catalog from a base URL")
    sub = parser.add_subparsers(dest="command", required=True)

    crawl = sub.add_parser("crawl", help="Crawl a site and write it to Postgres")
    crawl.add_argument("--url", required=True, help="Base URL, for example https://www2.hm.com/en_us")
    crawl.add_argument("--max-pages", type=int, default=None, help="Page cap for this run (default from settings)")

    serve = sub.add_parser("serve", help="Run the HTTP API")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)

    args = parser.parse_args(argv)
    configure_logging()
    if args.command == "serve":
        import uvicorn

        uvicorn.run("ingest.server:app", host=args.host, port=args.port, reload=False)
        return 0

    try:
        run_id = start_crawl(args.url, args.max_pages, get_settings())
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        execute_crawl(run_id)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
    with session_scope() as session:
        run = session.get(CrawlRun, run_id)
        if run is None:
            print("Crawl run disappeared before it could be reported", file=sys.stderr)
            return 1
        print(
            f"run {run.id} {run.status} pages={run.pages_fetched} "
            f"products={run.products_upserted} categories={run.categories_upserted} "
            f"errors={run.error_count}"
        )
        if run.error_message:
            print(run.error_message, file=sys.stderr)
        return 0 if run.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
