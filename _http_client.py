import json
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
import urllib.request
import urllib.error


class RateLimitExceeded(RuntimeError):
    """Raised when we determine a response indicates rate limiting and retries are exhausted."""

    def __init__(self, url: str, status: int, message: str = "", retry_after: Optional[float] = None):
        super().__init__(f"Rate limit exceeded (HTTP {status}) for {url}: {message}")
        self.url = url
        self.status = status
        self.retry_after = retry_after


@dataclass
class HttpClient:
    """HTTP client with rate-limit-aware retry logic (stdlib only)."""
    max_retries: int = 5
    max_sleep: float = 60.0
    timeout: float = 15.0
    user_agent: str = "HttpClient/1.0"

    # -------- Public API --------

    def get_json(self, url: str, headers: Optional[Dict[str, str]] = None) -> Any:
        """GET a URL and parse JSON if possible; otherwise return text."""
        ctype, text = self._do_request("GET", url, headers=headers)
        return self._json_or_text(ctype, text)

    def get_text(self, url: str, headers: Optional[Dict[str, str]] = None) -> str:
        """GET a URL and return text (JSON responses are serialized to text)."""
        ctype, text = self._do_request("GET", url, headers=headers)
        if "application/json" in (ctype or "") and text:
            # Coerce JSON to a compact string for text consumers
            try:
                obj = json.loads(text)
                return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
            except Exception:  # noqa
                pass
        return text

    def request(self, method: str, url: str, headers: Optional[Dict[str, str]] = None,
                data: Optional[bytes] = None) -> Tuple[str, str]:
        """
        Generic request. Returns (content_type, text_body).
        Method should be 'GET' for most rate-limited APIs; POST works too.
        """
        return self._do_request(method.upper(), url, headers=headers, data=data)

    # -------- Core logic --------

    def _do_request(self, method: str, url: str, headers: Optional[Dict[str, str]],
                    data: Optional[bytes] = None) -> tuple[str | Any, Any] | None:
        attempt = 0
        hdrs = dict(headers or {})
        hdrs.setdefault("User-Agent", self.user_agent)

        while True:
            try:
                req = urllib.request.Request(url, headers=hdrs, method=method, data=data)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    ctype = resp.headers.get("Content-Type", "") or ""
                    body = resp.read().decode("utf-8", "replace")
                    return ctype, body

            except urllib.error.HTTPError as e:
                # Read body safely (HTTPError is also a file-like)
                ctype, text = self._decode_error_body(e)

                if self._is_rate_limited(e.code, e.headers, text):
                    if attempt >= self.max_retries:
                        raise RateLimitExceeded(
                            e.url, e.code, message=(text or "")[:300],
                            retry_after=self._decide_sleep_seconds(e.headers, attempt)
                        ) from None

                    sleep_s = self._decide_sleep_seconds(e.headers, attempt)
                    time.sleep(max(0.0, sleep_s))
                    attempt += 1
                    continue

                # Not a rate-limit case -> bubble up with compact message
                msg = (text or "").strip()[:300]
                raise urllib.error.HTTPError(e.url, e.code, f"{e.reason} (body: {msg})", e.headers, e.fp)

            except urllib.error.URLError:
                # Network/transient: retry with backoff
                if attempt >= self.max_retries:
                    raise
                sleep_s = min(self.max_sleep, (2 ** attempt) + random.uniform(0, 0.5))
                time.sleep(sleep_s)
                attempt += 1
                continue

    # -------- Helpers --------

    @staticmethod
    def _json_or_text(ctype: str, text: str) -> Any:
        if "application/json" in (ctype or ""):
            return json.loads(text)
        try:
            return json.loads(text)
        except Exception:  # noqa
            return text

    @staticmethod
    def _decode_error_body(e: urllib.error.HTTPError) -> Tuple[str, str]:
        try:
            ctype = e.headers.get("Content-Type", "") if e.headers else ""
            body = e.read().decode("utf-8", "replace") if e.fp else ""
            return ctype or "", body or ""
        except Exception:  # noqa
            return "", ""

    @staticmethod
    def _looks_like_secondary_rl(body_text: str) -> bool:
        s = (body_text or "").lower()
        return ("secondary rate limit" in s) or ("abuse detection" in s)

    @staticmethod
    def _parse_retry_after(headers: Optional[Dict[str, str]]) -> Optional[float]:
        if not headers:
            return None
        ra = headers.get("Retry-After")
        if not ra:
            return None
        ra = ra.strip()
        if ra.isdigit():
            return max(0.0, float(ra))
        # RFC 7231 IMF-fixdate, e.g., 'Wed, 21 Oct 2015 07:28:00 GMT'
        try:
            dt = datetime.strptime(ra, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
            return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
        except Exception:  # noqa
            return None

    @staticmethod
    def _parse_reset_epoch(headers: Optional[Dict[str, str]]) -> Optional[float]:
        if not headers:
            return None
        for k in ("X-RateLimit-Reset", "RateLimit-Reset"):
            v = headers.get(k)
            if v and v.strip().isdigit():
                reset_ts = int(v.strip())
                return max(0.0, reset_ts - int(time.time()))
        return None

    def _is_rate_limited(self, status: int, headers, body_text: Optional[str]) -> bool:
        if status == 429:
            return True
        if status == 403:
            h = {k.lower(): v for k, v in (headers.items() if headers else [])}
            if h.get("x-ratelimit-remaining") == "0":
                return True
            if "ratelimit-remaining" in h and h["ratelimit-remaining"] == "0":
                return True
            if self._looks_like_secondary_rl(body_text or ""):
                return True
        return False

    def _decide_sleep_seconds(self, headers, attempt: int) -> float:
        # Prefer explicit server hints
        wait = self._parse_retry_after(headers)
        if wait is None:
            wait = self._parse_reset_epoch(headers)
        if wait is not None:
            return min(self.max_sleep, max(0.0, wait))
        # Fallback: exponential backoff with jitter
        return min(self.max_sleep, (2 ** attempt) + random.uniform(0, 0.5))
