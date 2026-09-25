"""Test helpers for connectors: point an ApiClient at a fake API for the duration of a test.

    from agentkit.tools.testing import mock_api

    def test_tracking(settings):
        with mock_api(CARRIER, {"GET /shipments/T1": {"status": "delivered"}}) as calls:
            ...  # run the agent
        assert calls[0].url.path.endswith("/shipments/T1")
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Union

import httpx

from .auth import NoAuth
from .http import ApiClient

__all__ = ["mock_api"]

Handler = Union[Callable[[httpx.Request], httpx.Response], Mapping[str, Any]]


def _route_table(routes: Mapping[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        for key, value in routes.items():
            method, _, suffix = key.partition(" ")
            if request.method == method.upper() and request.url.path.endswith(suffix):
                if isinstance(value, httpx.Response):
                    return value
                if isinstance(value, tuple):
                    status, body = value
                    return httpx.Response(status, json=body)
                return httpx.Response(200, json=value)
        return httpx.Response(404, json={"message": f"no mock for {request.method} {request.url.path}"})

    return handler


@contextmanager
def mock_api(client: ApiClient, handler: Handler) -> Iterator[list[httpx.Request]]:
    """Swap ``client``'s transport and auth for the block; yields the list of requests made.

    ``handler`` is a function ``request -> httpx.Response`` or a route table
    ``{"GET /path/suffix": body | (status, body) | httpx.Response}``.
    """
    fn = _route_table(handler) if isinstance(handler, Mapping) else handler
    calls: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return fn(request)

    saved = (client._client, client._auth, client._sleep)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(recording))
    client._auth = NoAuth()

    async def _no_sleep(_):
        return None

    client._sleep = _no_sleep
    try:
        yield calls
    finally:
        client._client, client._auth, client._sleep = saved

