"""Bounded, single-origin HTML collection with conservative robots handling."""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

import requests

LOG = logging.getLogger(__name__)
AGENT = "TohaAIScraper"
MAX_BYTES = 2 * 1024 * 1024


class ScrapeError(RuntimeError):
    """An expected collection failure."""


def canonical(url: str) -> str:
    try:
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or not p.hostname or p.username is not None or p.password is not None or "\\" in url or any(ord(c) <= 32 for c in url):
            raise ValueError
        port = p.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        host = p.hostname.lower()
        host = f"[{host}]" if ":" in host else host.encode("idna").decode("ascii")
        default = 443 if p.scheme == "https" else 80
        authority = host if port in (None, default) else f"{host}:{port}"
        return urlunsplit((p.scheme, authority, p.path or "/", p.query, ""))
    except (ValueError, UnicodeError) as exc:
        raise ScrapeError("Expected a valid HTTP(S) URL without credentials") from exc


def origin(url: str) -> tuple[str, str]:
    p = urlsplit(canonical(url))
    return p.scheme, p.netloc


class RobotsPolicy:
    """Common robots directives; unsupported/ambiguous rules fail closed."""

    def __init__(self, text: str):
        groups = []
        agents, directives = [], []
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if directives:
                    groups.append((agents, directives))
                    agents, directives = [], []
                agents.append(value.lower())
            elif agents and key in ("allow", "disallow", "crawl-delay", "request-rate"):
                directives.append((key, value))
        if agents:
            groups.append((agents, directives))
        specific = [d for a, d in groups if AGENT.lower() in a]
        selected = specific or [d for a, d in groups if "*" in a]
        self.rules = []
        self.delay = 0.0
        for group in selected:
            for key, value in group:
                if key == "crawl-delay":
                    try:
                        delay = float(value)
                        if not 0 <= delay <= 60:
                            raise ValueError
                        self.delay = max(self.delay, delay)
                    except ValueError as exc:
                        raise ScrapeError("Unsupported robots crawl-delay; stopping") from exc
                elif key == "request-rate":
                    raise ScrapeError("robots request-rate is not supported; stopping")
                elif value:
                    if not value.startswith("/") or "%" in value or not value.isascii() or len(value) > 512 or value.count("*") > 3:
                        raise ScrapeError("Unsupported robots path encoding; stopping")
                    end = value.endswith("$")
                    pattern = value[:-1] if end else value
                    regex = "^" + re.escape(pattern).replace(r"\*", ".*") + ("$" if end else "")
                    self.rules.append((len(pattern.replace("*", "")), key == "allow", re.compile(regex)))

    def allows(self, url: str) -> bool:
        p = urlsplit(url)
        path = p.path + ("?" + p.query if p.query else "")
        # Avoid ambiguous normalization under any applicable path rules.
        if self.rules and (len(path) > 2048 or "//" in p.path or "%" in path or not path.isascii() or any(x in (".", "..") for x in p.path.split("/"))):
            return False
        matches = [(size, allow) for size, allow, pattern in self.rules if pattern.search(path)]
        return max(matches)[1] if matches else True


class Client:
    """Sequential HTTP client. Never follows redirects or retries access denials."""

    def __init__(self, start_url: str, *, delay: float = 1.0, timeout: float = 15.0, retries: int = 2):
        self.start_url = canonical(start_url)
        if not 1 <= delay <= 60 or not 1 <= timeout <= 60 or not 0 <= retries <= 3:
            raise ScrapeError("delay/timeout must be 1–60 seconds; retries 0–3")
        self.delay, self.timeout, self.retries = delay, timeout, retries
        self.last_request = None
        self.policy = None
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers["User-Agent"] = f"{AGENT}/0.1 (+https://github.com/TohaAI-Lab/web-scraping-toolkit)"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.session.close()

    @staticmethod
    def retry_wait(header: str | None, attempt: int) -> float:
        wait = float(2 ** attempt)
        if header:
            try:
                seconds = int(header)
            except ValueError:
                try:
                    seconds = (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError, OverflowError) as exc:
                    raise ScrapeError("Invalid Retry-After; stopping") from exc
            wait = max(wait, seconds)
        if wait > 60:
            raise ScrapeError("Retry-After exceeds 60 seconds; stopping")
        return wait

    def _get(self, url: str, *, robots: bool = False) -> str | None:
        if origin(url) != origin(self.start_url):
            raise ScrapeError("Cross-origin request blocked")
        for attempt in range(self.retries + 1):
            if self.last_request is not None:
                time.sleep(max(0.0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                with self.session.get(url, timeout=self.timeout, allow_redirects=False, stream=True) as response:
                    status = response.status_code
                    if robots and status in (404, 410):
                        return None
                    if status in (429, 500, 502, 503, 504) and attempt < self.retries:
                        wait = self.retry_wait(response.headers.get("Retry-After"), attempt)
                    elif status != 200:
                        raise ScrapeError(f"HTTP {status}; stopping (redirects are not followed)")
                    else:
                        kind = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                        allowed = ("text/plain",) if robots else ("text/html", "application/xhtml+xml")
                        if kind not in allowed:
                            raise ScrapeError(f"Unsupported response content type: {kind or 'missing'}")
                        chunks = bytearray()
                        for chunk in response.iter_content(16384):
                            chunks.extend(chunk)
                            if len(chunks) > MAX_BYTES:
                                raise ScrapeError("Response exceeds 2 MiB limit")
                        encoding = "utf-8-sig" if robots else (response.encoding if "charset=" in response.headers.get("Content-Type", "").lower() and response.encoding else "utf-8")
                        try:
                            return chunks.decode(encoding, errors="strict")
                        except (UnicodeError, LookupError) as exc:
                            raise ScrapeError("Response encoding could not be decoded") from exc
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt == self.retries:
                    raise ScrapeError("Network request failed") from exc
                wait = self.retry_wait(None, attempt)
            except requests.RequestException as exc:
                raise ScrapeError("Network request failed") from exc
            LOG.warning("Retrying request after %.1f seconds", wait)
            time.sleep(wait)
        raise ScrapeError("Request failed")

    def fetch(self, url: str) -> str:
        url = canonical(url)
        if origin(url) != origin(self.start_url):
            raise ScrapeError("Cross-origin pagination blocked")
        if self.policy is None:
            scheme, authority = origin(self.start_url)
            text = self._get(f"{scheme}://{authority}/robots.txt", robots=True)
            self.policy = RobotsPolicy(text or "")
            self.delay = max(self.delay, self.policy.delay)
        if not self.policy.allows(url):
            raise ScrapeError("robots.txt disallows this URL")
        result = self._get(url)
        assert result is not None
        return result
