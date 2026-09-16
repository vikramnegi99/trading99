"""Shared HTTP + caching utilities: retries, timeouts, rate limiting."""
import json
import logging
import threading
import time
from typing import Optional

logger = logging.getLogger("utils.http")

try:
    import requests
except ImportError:  # allows unit tests without requests installed
    requests = None


class TTLCache:
    """Tiny thread-safe TTL cache used to avoid duplicate fetches."""

    def __init__(self, ttl_seconds: int = 300, max_entries: int = 2048):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._data = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            expires, value = item
            if time.time() > expires:
                self._data.pop(key, None)
                return None
            return value

    def set(self, key, value):
        with self._lock:
            if len(self._data) >= self.max_entries:
                # drop oldest ~10% to bound memory
                for k in list(self._data)[: max(1, self.max_entries // 10)]:
                    self._data.pop(k, None)
            self._data[key] = (time.time() + self.ttl, value)


class RateLimiter:
    """Minimum-interval throttle between outgoing calls."""

    def __init__(self, min_interval: float = 0.25):
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            delta = time.time() - self._last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last = time.time()


class HttpClient:
    """GET/POST with retries, exponential backoff, timeout, rate limit, cache."""

    def __init__(self, timeout: float = 15.0, retries: int = 3,
                 rate_limit: float = 0.25, cache: Optional[TTLCache] = None):
        if requests is None:
            raise RuntimeError("requests library not installed")
        self.timeout = timeout
        self.retries = retries
        self.limiter = RateLimiter(rate_limit)
        self.cache = cache or TTLCache(ttl_seconds=60)

    def get(self, url: str, params: dict = None, use_cache: bool = True):
        cache_key = json.dumps(["GET", url, params or {}], sort_keys=True)
        if use_cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached
        last_err = None
        for attempt in range(self.retries):
            try:
                self.limiter.wait()
                resp = requests.get(url, params=params, timeout=self.timeout,
                                     headers={"User-Agent": "paper-trading-agent/1.0"})
                if resp.status_code == 429:  # rate limited by remote
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                data = resp.json()
                if use_cache:
                    self.cache.set(cache_key, data)
                return data
            except Exception as exc:  # noqa: BLE001 - fail-safe retry loop
                last_err = exc
                logger.warning("GET %s failed (attempt %s/%s): %s",
                              url, attempt + 1, self.retries, exc)
                time.sleep(min(2 ** attempt, 8))
        raise ConnectionError(f"GET {url} failed after {self.retries} attempts: {last_err}")

    def post(self, url: str, json_body: dict = None, headers: dict = None):
        self.limiter.wait()
        resp = requests.post(url, json=json_body, headers=headers,
                             timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()
