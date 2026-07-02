"""Generic HTTP retry/backoff wrapper around `requests.request`.

Provider-agnostic: this module has no knowledge of Microsoft Graph, iCloud
CardDAV, or any other API shape. It retries requests that come back with a
status code in `RETRYABLE_STATUS_CODES`, honoring a `Retry-After` response
header when present and falling back to capped exponential backoff otherwise.
"""

import time

import requests

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def request_with_retry(method: str, url: str, max_attempts: int = 5, sleep=time.sleep, **kwargs) -> requests.Response:
    attempt = 0
    while True:
        attempt += 1
        response = requests.request(method, url, **kwargs)
        if response.status_code not in RETRYABLE_STATUS_CODES or attempt >= max_attempts:
            return response
        retry_after = response.headers.get("Retry-After")
        delay = float(retry_after) if retry_after else min(2 ** attempt, 30)
        sleep(delay)
