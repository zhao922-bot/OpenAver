"""
core.metatube.errors — Exception hierarchy for MetatubeHttpClient.

All exceptions inherit MetatubeError so callers can catch broadly with
`except MetatubeError` or narrowly with specific subclasses.

Routing vs probe semantics (CD-63a-6):
  MetatubeUnavailable  — routing: mark_failed; probe: available=false
  MetatubeNotFound     — routing: NOT a failure (番號不在此源);
                         probe: available=false (canary 404 = scraper broken)
  MetatubeAuthError    — routing: toast (token wrong); probe: same
  MetatubeClientError  — routing: not a failure; probe: available=false (保守)
  MetatubeProtocolError— routing: treat as no-data; probe: available=false
"""


class MetatubeError(Exception):
    """Base exception for all Metatube HTTP client errors."""


class MetatubeUnavailable(MetatubeError):
    """
    Server unreachable or returned 5xx.

    Triggers: requests.Timeout / requests.ConnectionError / HTTP 5xx.
    Routing action: mark_failed (available=false).
    """


class MetatubeNotFound(MetatubeError):
    """
    HTTP 404 — requested resource not found.

    Routing action: NOT a failure (movie simply absent from this provider).
    Probe action: available=false (canary known-good 404 means scraper broken).
    """


class MetatubeAuthError(MetatubeError):
    """
    HTTP 401 — authentication failed (bad or missing token).

    Routing action: surface toast to user; do NOT mark_failed.
    """


class MetatubeClientError(MetatubeError):
    """
    HTTP 4xx (excluding 401/404) — client-side error (e.g. 400, 422).

    Routing action: not a failure.
    Probe action: available=false (canary should never return 4xx — treat conservatively).
    """


class MetatubeProtocolError(MetatubeError):
    """
    HTTP 200 but response is not valid JSON, or JSON lacks the 'data' key.

    Does NOT trigger on data=null (null is a legitimate empty value).
    Routing action: treat as no-data for this request.
    Probe action: available=false.
    """
