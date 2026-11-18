"""CLI entry point for TohaAI Lab Web Scraping Toolkit."""
import argparse
import logging
from pathlib import Path

from soupsieve import SelectorSyntaxError

from scraping.core import Client, ScrapeError
from scraping.extract import collect, parse_fields
from scraping.export import export


def main(argv=None):
    parser = argparse.ArgumentParser(description="Collect permitted static HTML data into CSV or JSON.")
    parser.add_argument("url")
    parser.add_argument("--item", required=True, help="CSS selector for repeating records")
    parser.add_argument("--field", action="append", required=True, help="NAME=CSS or NAME=CSS::attr(href); repeatable")
    parser.add_argument("--next", dest="next_selector", help="CSS selector for next-page link")
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--format", choices=("csv", "json"), default="json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        fields = parse_fields(args.field)
        if args.output.suffix.lower() != "." + args.format:
            raise ValueError("Output extension must match --format")
        with Client(args.url, delay=args.delay, timeout=args.timeout, retries=args.retries) as client:
            rows = collect(client, client.start_url, args.item, fields, args.next_selector, args.max_pages)
        export(rows, list(fields), args.output, args.format)
        logging.info("Exported %d unique records", len(rows))
        return 0
    except (ScrapeError, ValueError, SelectorSyntaxError, OSError) as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
