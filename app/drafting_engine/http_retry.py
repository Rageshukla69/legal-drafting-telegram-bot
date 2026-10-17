"""Shared HTTP resilience helper.

Every outbound AI/API call in this project (Gemini, Azure AI Foundry, Azure Speech)
is a network request that can transiently fail with 429/500/502/503/504 under provider
load. This helper provides a requests.Session whose adapter retries those status
codes with exponential backoff before ever raising.
"""
from __future__ import annotations

import inspect
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
    retry_kwargs = {
        "total": max_retries,
        "connect": max_retries,
        "read": max_retries,
        "status": max_retries,
        "backoff_factor": backoff_factor,
        "status_forcelist": sorted(RETRYABLE_STATUS),
        "raise_on_status": False,
    }
    sig = inspect.signature(Retry.__init__)
    if "allowed_methods" in sig.parameters:
        retry_kwargs["allowed_methods"] = methods
    elif "method_whitelist" in sig.parameters:
        retry_kwargs["method_whitelist"] = methods

    if "backoff_max" in sig.parameters:
        retry_kwargs["backoff_max"] = backoff_max
    if "backoff_jitter" in sig.parameters:
        retry_kwargs["backoff_jitter"] = 0.5
    if "respect_retry_after_header" in sig.parameters:
        retry_kwargs["respect_retry_after_header"] = True

    retry = Retry(**retry_kwargs)
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
