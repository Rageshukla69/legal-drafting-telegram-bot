"""Shared HTTP resilience helper.

Every outbound AI/API call in this project (Gemini, Azure Speech) is a network
request that can transiently fail with 429/500/502/503/504 under provider
load. A single unlucky attempt should not fail a user's request. This module
gives every caller a `requests.Session` whose adapter retries those status
codes with exponential backoff + jitter before ever raising, following
Google's own documented guidance for the Gemini API:
https://ai.google.dev/gemini-api/docs/troubleshooting
"""
from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def build_retrying_session(
    *,
    max_retries: int = 3,
    backoff_factor: float = 1.5,
    backoff_max: float = 60.0,
    methods: frozenset[str] = frozenset({"POST"}),
) -> requests.Session:
    retry = Retry(
        total=max_retries,
        connect=max_retries,
        read=max_retries,
        status=max_retries,
        backoff_factor=backoff_factor,
        backoff_max=backoff_max,
        backoff_jitter=0.5,
        status_forcelist=sorted(RETRYABLE_STATUS),
        allowed_methods=methods,
        respect_retry_after_header=True,
        # Return the final failing response instead of raising, so callers can
        # inspect the status code/body and produce a clean, specific error.
        raise_on_status=False,
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
