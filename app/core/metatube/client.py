"""
core.metatube.client — Synchronous HTTP client for a Metatube server.

Implements three API methods against a self-hosted metatube instance:
  - list_providers()  → dict[str, str]
  - get_info()        → dict | None
  - search()          → list[dict]

Plus a pure-function FANZA multi-result disambiguator:
  - pick_movie_result() → dict | None

Design decisions (CD-63a-6/7/8):
  - Synchronous requests (aligns with codebase-wide scraper pattern).
  - Per-instance requests.Session (thread-safety: caller creates one client per task).
  - Bearer token only sent when token is non-empty.
  - base_url trailing slash stripped to avoid double-slash paths.
  - timeout=20s (wider than builtin 15s; metatube proxies to upstream scrapers).
  - Status-code classification done BEFORE json() to handle HTML error pages.
  - data=null is a legitimate empty response — not a ProtocolError.
"""

import json
import urllib.parse

import requests

from core.logger import get_logger
from core.metatube.errors import (
    MetatubeAuthError,
    MetatubeClientError,
    MetatubeNotFound,
    MetatubeProtocolError,
    MetatubeUnavailable,
)

logger = get_logger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.5 Safari/605.1.15"
)

# Sentinel to distinguish "key absent" from "key present but value is None"
_SENTINEL = object()


class MetatubeHttpClient:
    """
    Synchronous HTTP client for a self-hosted Metatube server.

    Args:
        base_url: Server base URL (e.g. "http://192.168.1.10:8900").
        token:    Optional Bearer token. Empty string treated same as None.
        timeout:  Request timeout in seconds (default 20).
    """

    def __init__(self, base_url: str, token: str | None = None, timeout: int = 20) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        })
        # Only set Bearer when token is a non-empty string
        if token:
            self._session.headers.update({"Authorization": f"Bearer {token}"})

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    def list_providers(self) -> dict:
        """
        GET /v1/providers

        Returns dict[str, str] mapping provider name → provider base URL.
        actor_providers are discarded (spec Non-Goal: no actor federation).
        Returns {} when the server returns data=null.
        """
        data = self._get_data("/v1/providers", params=None)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise MetatubeProtocolError(
                f"list_providers: expected 'data' to be a JSON object, "
                f"got {type(data).__name__} instead."
            )
        if "movie_providers" not in data:
            raise MetatubeProtocolError(
                "list_providers: response 'data' missing required 'movie_providers' key. "
                f"data keys: {list(data.keys())}"
            )
        movie_providers = data["movie_providers"]
        if not isinstance(movie_providers, dict):
            raise MetatubeProtocolError(
                f"list_providers: expected 'movie_providers' to be a JSON object, "
                f"got {type(movie_providers).__name__} instead."
            )
        return movie_providers

    def get_info(self, provider: str, movie_id: str, lazy: bool = True) -> dict | None:
        """
        GET /v1/movies/{provider}/{movie_id}?lazy=true

        Args:
            provider: Provider name (case-sensitive, e.g. "FANZA").
            movie_id: Movie ID on the provider (URL-encoded in the path).
            lazy:     Use server-side cache (default True).

        Returns:
            dict with movie info, or None if server returned data=null.
        """
        encoded_id = urllib.parse.quote(movie_id, safe="")
        path = f"/v1/movies/{provider}/{encoded_id}"
        params = {"lazy": "true" if lazy else "false"}
        data = self._get_data(path, params=params)
        if data is not None and not isinstance(data, dict):
            raise MetatubeProtocolError(
                f"get_info: expected 'data' to be a JSON object or null, "
                f"got {type(data).__name__} instead."
            )
        return data

    def search(self, provider: str, q: str) -> list:
        """
        GET /v1/movies/search?q={q}&provider={provider}&fallback=false

        Always sends 'provider' param to prevent triggering SearchMovieAll broadcast.
        Sends 'fallback=false' to enforce source isolation: the upstream metatube
        route defaults Fallback=true, so a provider-scoped search with no match would
        otherwise return ANOTHER provider's hit — corrupting explicit single-source
        picks (US8) and auto fan-out source attribution. We want [] when the requested
        provider has no match, never a foreign provider's result. (Go strconv.ParseBool
        accepts the lowercase string "false".)

        Returns:
            list of movie result dicts, or [] if server returned data=null.
        """
        params = {"q": q, "provider": provider, "fallback": "false"}
        data = self._get_data("/v1/movies/search", params=params)
        if data is None:
            return []
        if not isinstance(data, list):
            raise MetatubeProtocolError(
                f"search: expected 'data' to be a JSON array or null, "
                f"got {type(data).__name__} instead."
            )
        return data

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_data(self, path: str, params) -> dict | list | None:
        """
        Shared request + classification logic for all three methods.

        Ordering (important — avoids parsing HTML error pages as JSON):
          1. Catch network errors → MetatubeUnavailable
          2. Classify by status_code (no json() yet) → raise appropriate exception
          3. Only for status 200: parse json() → check envelope → return data

        SSRF guard: redirects are NOT followed (allow_redirects=False). A public,
        validated metatube host could otherwise 30x-redirect to a loopback / internal
        address, bypassing validate_metatube_url() (which only checks the original URL).
        metatube is a direct JSON REST API and never legitimately redirects, so any 3xx
        is treated as a protocol error rather than followed.

        Returns:
            The 'data' value from the envelope (may be None for empty responses).

        Raises:
            MetatubeUnavailable:   ConnectionError / Timeout / 5xx
            MetatubeAuthError:     401
            MetatubeNotFound:      404
            MetatubeClientError:   other 4xx
            MetatubeProtocolError: 3xx redirect (SSRF guard), or 200 + invalid JSON / missing 'data' key
        """
        url = f"{self._base_url}{path}"
        logger.debug("metatube GET %s params=%s", url, params)

        try:
            resp = self._session.get(
                url, params=params, timeout=self._timeout, allow_redirects=False
            )
        except requests.RequestException as exc:
            logger.warning("metatube unavailable: %s — %s", url, type(exc).__name__)
            raise MetatubeUnavailable("Network error reaching metatube server") from exc

        status = resp.status_code

        # --- Status-code classification (before JSON parsing) ---
        # SSRF guard: never follow redirects — a 3xx could point at loopback / internal
        # hosts that validate_metatube_url() never saw. Reject instead of following.
        if 300 <= status < 400:
            raise MetatubeProtocolError(
                f"Unexpected redirect from {url} (HTTP {status}); "
                "redirects are not allowed (SSRF guard)."
            )
        if status == 401:
            raise MetatubeAuthError(f"Authentication failed for {url} (HTTP 401)")
        if status == 404:
            raise MetatubeNotFound(f"Not found: {url} (HTTP 404)")
        if 400 <= status < 500:
            raise MetatubeClientError(f"Client error for {url} (HTTP {status})")
        if 500 <= status < 600:
            raise MetatubeUnavailable(f"Server error for {url} (HTTP {status})")

        # --- status 200: parse JSON envelope ---
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise MetatubeProtocolError(
                f"Invalid JSON from {url}: {exc}"
            ) from exc

        # Envelope must be a JSON object (dict); list/scalar bodies are protocol errors
        if not isinstance(body, dict):
            raise MetatubeProtocolError(
                f"Response from {url} is not a JSON object (got {type(body).__name__}). "
                f"Expected {{\"data\": ...}} envelope."
            )

        # Check envelope — 'data' key must exist (value may be None)
        sentinel = _SENTINEL
        data = body.get("data", sentinel)
        if data is sentinel:
            raise MetatubeProtocolError(
                f"Response from {url} missing 'data' key. "
                f"Body keys: {list(body.keys())}"
            )

        return data  # may be None (legitimate empty response)


# ------------------------------------------------------------------
# Module-level pure function (placed outside class, column 0)
# ------------------------------------------------------------------

def pick_movie_result(results: list) -> dict | None:
    """
    Disambiguate multiple search results for a movie number.

    Rules (CD-63a-8):
      1. Empty list → None.
      2. Find first result whose 'homepage' contains 'video.dmm.co.jp'
         (FANZA streaming version preferred over DVD/Blu-ray variants).
      3. No match → return results[0] (metatube already sorts by priority).

    Args:
        results: List of movie search result dicts from search().

    Returns:
        Best matching dict, or None for empty input.
    """
    if not results:
        return None

    for r in results:
        if "video.dmm.co.jp" in r.get("homepage", ""):
            return r

    return results[0]
