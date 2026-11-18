"""CSS-selector extraction and bounded same-origin pagination."""
from __future__ import annotations

import logging
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from soupsieve import compile as compile_selector

from scraping.core import ScrapeError, canonical, origin

LOG = logging.getLogger(__name__)


def parse_fields(values: list[str]) -> dict[str, tuple[str, str | None]]:
    fields = {}
    for value in values:
        name, sep, spec = value.partition("=")
        selector, marker, attr = spec.partition("::attr(")
        if not sep or not name.isidentifier() or name in fields or not selector.strip():
            raise ValueError("Fields must use unique names: NAME=CSS or NAME=CSS::attr(href)")
        if marker and (not attr.endswith(")") or not attr[:-1].strip()):
            raise ValueError("Invalid ::attr(...) suffix")
        compile_selector(selector)
        fields[name] = (selector, attr[:-1].strip() if marker else None)
    if not fields:
        raise ValueError("At least one field is required")
    return fields


def extract(html: str, item: str, fields: dict, next_selector: str | None = None):
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for node in soup.select(item):
        row = {}
        for name, (selector, attr) in fields.items():
            child = node.select_one(selector)
            if child is None:
                row[name] = ""
            elif attr:
                value = child.get(attr, "")
                row[name] = " ".join(value) if isinstance(value, list) else str(value)
            else:
                row[name] = child.get_text(" ", strip=True)
        rows.append(row)
    link = soup.select_one(next_selector) if next_selector else None
    href = link.get("href") if link else None
    return rows, href if isinstance(href, str) and href else None


def collect(client, start_url: str, item: str, fields: dict, next_selector: str | None, max_pages: int = 1):
    if not 1 <= max_pages <= 100:
        raise ValueError("max_pages must be 1–100")
    compile_selector(item)
    if next_selector:
        compile_selector(next_selector)
    url = canonical(start_url)
    start_origin = origin(url)
    seen_pages, seen_rows, result = set(), set(), []
    for _ in range(max_pages):
        if url in seen_pages:
            LOG.warning("Pagination cycle detected; stopping")
            break
        seen_pages.add(url)
        rows, href = extract(client.fetch(url), item, fields, next_selector)
        LOG.info("Page %d: %d matching records", len(seen_pages), len(rows))
        for row in rows:
            key = tuple(row.values())
            if key not in seen_rows:
                seen_rows.add(key)
                result.append(row)
                if len(result) > 100000:
                    raise ScrapeError("Record limit exceeded (100000)")
        if not href or len(seen_pages) == max_pages:
            break
        url = canonical(urljoin(url, href))
        if origin(url) != start_origin:
            raise ScrapeError("Cross-origin pagination blocked")
    return result
