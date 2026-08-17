"""Shared HTTP session: descriptive UA, retries with backoff, per-host delay."""

from __future__ import annotations

import logging
import threading
import time

import requests

import config

log = logging.getLogger(__name__)


class Fetcher:
    """A polite, retrying HTTP client with an optional fixed inter-request delay.

    One instance per source so each can carry its own rate limit. Thread-safe
    so the enrichment pool can share a single instance.
    """

    def __init__(self, delay: float = 0.0, timeout: int = None,
                 retries: int = None, referer: str = ""):
        self.delay = delay
        self.timeout = timeout if timeout is not None else config.HTTP_TIMEOUT
        self.retries = retries if retries is not None else config.HTTP_RETRIES
        self._lock = threading.Lock()
        self._last = 0.0
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config.USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        })
        if referer:
            self.session.headers["Referer"] = referer

    def _wait(self):
        if self.delay <= 0:
            return
        with self._lock:
            gap = time.monotonic() - self._last
            if gap < self.delay:
                time.sleep(self.delay - gap)
            self._last = time.monotonic()

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        last_err = None
        for attempt in range(self.retries):
            self._wait()
            try:
                resp = self.session.request(
                    method, url, timeout=self.timeout, **kwargs
                )
                # Retry transient server-side failures only; a 404 is an answer.
                if resp.status_code >= 500:
                    raise requests.HTTPError(
                        f"{resp.status_code} from {url}", response=resp
                    )
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:
                last_err = exc
                if attempt < self.retries - 1:
                    sleep_for = config.HTTP_BACKOFF ** (attempt + 1)
                    log.warning(
                        "%s %s failed (%s); retrying in %.0fs",
                        method, url, exc, sleep_for,
                    )
                    time.sleep(sleep_for)
        raise last_err

    def get(self, url: str, **kwargs) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self.request("POST", url, **kwargs)


class ScrapeError(RuntimeError):
    """Raised when a source returns implausibly little data.

    Treated as a hard failure by main.py: an empty source means the endpoint
    or markup changed, and silently writing an empty Sheet would be worse than
    failing loudly.
    """
