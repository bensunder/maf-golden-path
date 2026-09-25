"""The real Azure AI Search SDK against a local HTTP server: what actually goes over the wire.

The fake index proves trimming logic; this proves the SDK serialises our query the way the service expects
(filter, pre-filtered vector query, semantic ranker, selected fields). MAF hit an enum-serialisation bug at
exactly this layer, so it's worth a test."""

import json
import socket

import pytest
from aiohttp import web
from azure.core.credentials import AzureKeyCredential
from azure.search.documents.aio import SearchClient

from agentkit.knowledge import KnowledgeBase, KnowledgeSettings, StaticGroups
from agentkit.knowledge.testing import fake_embedder


@pytest.fixture
async def search_server():
    requests = []

    async def search(request: web.Request) -> web.Response:
        requests.append({"path": request.path, "query": dict(request.query), "body": await request.json(),
                         "api_key": request.headers.get("api-key")})
        return web.json_response({"value": [{"@search.score": 1.2, "@search.rerankerScore": 3.1, "id": "c1",
                                             "doc_id": "refund-policy", "title": "Refund policy",
                                             "url": "https://intranet/refunds", "content": "Over $50 needs a lead."}]})

    app = web.Application()
    app.router.add_post("/indexes('{index}')/docs/search.post.search", search)
    app.router.add_post("/indexes/{index}/docs/search.post.search", search)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    yield f"http://127.0.0.1:{port}", requests
    await runner.cleanup()


async def test_query_on_the_wire(search_server):
    endpoint, requests = search_server
    async with SearchClient(endpoint, "knowledge", AzureKeyCredential("k")) as client:
        kb = KnowledgeBase(KnowledgeSettings(_env_file=None), search_client=client, embed=fake_embedder(),
                           groups=StaticGroups({"sam": ["g-support"]}))
        (passage,) = await kb.search("who approves refunds", user_id="sam")
    body = requests[0]["body"]
    assert body["filter"] == "groups/any(g: search.in(g, 'all-users,g-support', ','))"
    assert body["search"] == "who approves refunds" and body["top"] == 5
    assert body["queryType"] == "semantic" and body["semanticConfiguration"] == "default"
    assert body["vectorFilterMode"] == "preFilter"
    (vector,) = body["vectorQueries"]
    assert vector["kind"] == "vector" and vector["fields"] == "content_vector" and len(vector["vector"]) == 8
    assert set(body["select"].split(",")) == {"id", "doc_id", "title", "url", "content"}
    assert passage.doc_id == "refund-policy" and passage.score == 3.1  # reranker score preferred
    assert json.dumps(body).count("QueryType") == 0  # enum values, not reprs
