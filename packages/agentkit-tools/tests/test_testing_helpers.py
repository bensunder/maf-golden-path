import httpx

from agentkit.tools import ApiClient, ManagedIdentityAuth
from agentkit.tools.testing import mock_api


async def test_mock_api_route_table_and_restore():
    client = ApiClient("https://api.example.com", auth=ManagedIdentityAuth("api://x/.default"))
    original = client._client
    with mock_api(client, {"GET /items/1": {"id": 1}, "POST /items": (201, {"id": 2})}) as calls:
        assert await client.request("GET", "/items/1") == {"id": 1}
        assert await client.request("POST", "/items", json={}) == {"id": 2}
        try:
            await client.request("GET", "/nope")
        except Exception as exc:
            assert "404" in str(exc)
    assert [c.method for c in calls] == ["GET", "POST", "GET"]
    assert "Authorization" not in calls[0].headers  # real auth is bypassed in tests
    assert client._client is original


async def test_mock_api_with_function():
    client = ApiClient("https://api.example.com")
    with mock_api(client, lambda r: httpx.Response(200, json={"path": r.url.path})):
        assert await client.request("GET", "/a/b") == {"path": "/a/b"}
