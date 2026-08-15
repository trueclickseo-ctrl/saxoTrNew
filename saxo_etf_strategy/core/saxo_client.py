"""
Minimal Saxo OpenAPI HTTP client for the ETF module.

Design goal: this does NOT manage OAuth. Pass a `token_provider` callable
(returns a valid bearer token string). The ETF module reuses the parent
app's saxo_auth.get_valid_access_token via run_etf_bot._get_saxo_token —
zero coupling to the shares strategies.
"""

import time
import logging
from typing import Callable, Optional, Dict, Any

import requests

logger = logging.getLogger("etf_strategy.saxo_client")


class SaxoAPIError(Exception):
    def __init__(self, status_code: int, message: str, payload: Any = None):
        super().__init__(f"Saxo API error {status_code}: {message}")
        self.status_code = status_code
        self.payload     = payload


class SaxoClient:
    def __init__(
        self,
        base_url: str,
        token_provider: Callable[[], str],
        max_retries: int = 3,
        request_delay_sec: float = 0.20,
        timeout_sec: float = 20.0,
    ):
        self.base_url          = base_url.rstrip("/")
        self.token_provider    = token_provider
        self.max_retries       = max_retries
        self.request_delay_sec = request_delay_sec
        self.timeout_sec       = timeout_sec
        self._session          = requests.Session()

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token_provider()}",
            "Content-Type":  "application/json",
        }

    def _request(self, method: str, path: str,
                 params: Optional[dict] = None,
                 json_body: Optional[dict] = None) -> dict:
        url      = f"{self.base_url}{path}"
        last_exc = None

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._session.request(
                    method, url,
                    headers=self._headers(),
                    params=params,
                    json=json_body,
                    timeout=self.timeout_sec,
                )
                if resp.status_code == 429:
                    wait = float(resp.headers.get("Retry-After", 2 * attempt))
                    logger.warning(f"Rate limited on {path}, waiting {wait}s")
                    time.sleep(wait)
                    continue
                if resp.status_code >= 400:
                    body = resp.text
                    if len(body) > 400:
                        body = body[:400] + f"… [{len(body) - 400} chars truncated]"
                    raise SaxoAPIError(resp.status_code, body)
                time.sleep(self.request_delay_sec)
                return resp.json() if resp.text.strip() else {}

            except (requests.RequestException, SaxoAPIError) as exc:
                last_exc = exc
                logger.warning(f"Attempt {attempt}/{self.max_retries} failed for {path}: {exc}")
                time.sleep(min(2 ** attempt, 10))

        raise last_exc or SaxoAPIError(429, f"All {self.max_retries} retries rate-limited for {path}")

    def get(self, path: str, params: Optional[dict] = None) -> dict:
        return self._request("GET", path, params=params)

    def post(self, path: str, json_body: Optional[dict] = None) -> dict:
        return self._request("POST", path, json_body=json_body)

    def patch(self, path: str, json_body: Optional[dict] = None) -> dict:
        return self._request("PATCH", path, json_body=json_body)

    def delete(self, path: str, params: Optional[dict] = None) -> dict:
        return self._request("DELETE", path, params=params)
