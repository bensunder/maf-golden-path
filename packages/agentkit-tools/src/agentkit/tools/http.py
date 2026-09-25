"""Resilient async HTTP for tools: auth, retries, timeouts, trace propagation, model-friendly errors."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from opentelemetry import propagate

from .auth import AuthError, NoAuth, ToolAuth

__all__ = ["ApiClient", "ToolHttpError", "RetryPolicy"]

logger = logging.getLogger(__name__)

IDEMPOTENT = {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class ToolHttpError(Exception):
    """A downstream failure, phrased for the model (no stack traces, no secrets)."""


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    retry_methods: frozenset[str] = frozenset(IDEMPOTENT)

    def delay(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None and (header := response.headers.get("Retry-After")):
            try:
                return min(float(header), self.max_delay)
            except ValueError:
                try:
                    from datetime import datetime, timezone

                    wait = (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds()
                    return max(0.0, min(wait, self.max_delay))
                except (TypeError, ValueError):
                    pass
        return min(self.max_delay, self.base_delay * 2**attempt) * (0.5 + random.random() / 2)


_FRIENDLY = {
    400: "the request was rejected as invalid",
    401: "authentication failed",
    403: "access denied (the caller lacks permission)",
    404: "not found",
    409: "conflict with the current state",
    422: "the request data was invalid",
    429: "the service is rate limiting; try again later",
}


class ApiClient:
    """One downstream API. Share an instance across tools for connection pooling."""

    def __init__(
        self,
        base_url: str,
        *,
        auth: ToolAuth | None = None,
        timeout: float = 15.0,
        retry: RetryPolicy | None = None,
        headers: Mapping[str, str] | None = None,
        http_client: httpx.AsyncClient | None = None,
        sleep=asyncio.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._auth = auth or NoAuth()
        self._retry = retry or RetryPolicy()
        self._headers = dict(headers or {})
        self._client = http_client or httpx.AsyncClient(timeout=timeout)
        self._sleep = sleep

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        method = method.upper()
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            auth_headers = await self._auth.headers()
        except AuthError as exc:
            raise ToolHttpError(f"could not authenticate: {exc}") from exc
        request_headers = {"Accept": "application/json", **self._headers, **auth_headers, **dict(headers or {})}
        propagate.inject(request_headers)  # W3C traceparent: downstream spans join the agent's trace
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}

        last_error: str = "request failed"
        attempts = self._retry.attempts if method in self._retry.retry_methods else 1
        for attempt in range(attempts):
            response: httpx.Response | None = None
            try:
                response = await self._client.request(
                    method, url, params=clean_params, json=json, headers=request_headers
                )
            except httpx.TimeoutException:
                last_error = "the service timed out"
            except httpx.TransportError as exc:
                last_error = f"the service is unreachable ({type(exc).__name__})"
            else:
                if response.status_code < 400:
                    return self._decode(response)
                last_error = self._describe(response)
                if response.status_code not in RETRYABLE_STATUS:
                    break
            if attempt + 1 < attempts:
                await self._sleep(self._retry.delay(attempt, response))
        logger.warning("%s %s failed: %s", method, url, last_error)
        raise ToolHttpError(last_error)

    @staticmethod
    def _decode(response: httpx.Response) -> Any:
        if response.status_code == 204 or not response.content:
            return {"status": response.status_code}
        if "json" in response.headers.get("content-type", ""):
            return response.json()
        return response.text

    @staticmethod
    def _describe(response: httpx.Response) -> str:
        reason = _FRIENDLY.get(response.status_code, "the service returned an error")
        detail = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = str(body.get("message") or body.get("error_description") or body.get("title") or "")
                if not detail and isinstance(body.get("error"), dict):
                    detail = str(body["error"].get("message") or "")
        except ValueError:
            pass
        detail = detail[:200]
        return f"HTTP {response.status_code}: {reason}" + (f" ({detail})" if detail else "")

    async def aclose(self) -> None:
        await self._client.aclose()
