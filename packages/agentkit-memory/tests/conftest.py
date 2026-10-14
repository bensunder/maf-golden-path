import os
import shutil
import socket
import subprocess
import time
import uuid

import pytest
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

from agentkit.memory import reset_memory_services


@pytest.fixture(autouse=True)
def _fresh_services():
    reset_memory_services()
    yield
    reset_memory_services()


@pytest.fixture(scope="session")
def redis_url():
    if url := os.getenv("AGENTKIT_TEST_REDIS_URL"):
        yield url
        return
    binary = shutil.which("redis-server")
    if not binary:
        pytest.skip("no Redis available (install redis-server or set AGENTKIT_TEST_REDIS_URL)")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([binary, "--port", str(port), "--save", "", "--appendonly", "no"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    yield f"redis://127.0.0.1:{port}/0"
    proc.terminate()


@pytest.fixture
def restricted_redis(redis_url):
    """A Redis user with exactly the VPS stack's per-agent rights (deploy/vps/agentctl.py redis_acl):
    GET, SET, DEL, EVAL and PING on its own prefix only. Yields (url, prefix)."""
    from redis import Redis

    name, password = f"agent-{uuid.uuid4().hex[:8]}", uuid.uuid4().hex
    prefix = f"agentkit:{name}:"
    admin = Redis.from_url(redis_url, decode_responses=True)
    admin.execute_command("ACL", "SETUSER", name, "on", f">{password}", f"~{prefix}*", "resetchannels",
                          "-@all", "+get", "+set", "+del", "+eval", "+ping")
    host_port = redis_url.split("//", 1)[1].split("/", 1)[0]
    yield f"redis://{name}:{password}@{host_port}/0", prefix
    admin.execute_command("ACL", "DELUSER", name)
    admin.close()


@pytest.fixture
def fake_cosmos():
    return FakeCosmosContainer()


class FakeCosmosContainer:
    """Mimics azure.cosmos.aio.ContainerProxy for the memory store: _etag, IfNotModified, per-item ttl."""

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self.fail_with: Exception | None = None

    def _alive(self, item_id: str) -> dict | None:
        item = self.items.get(item_id)
        if item and item.get("ttl") and item["_created"] + item["ttl"] <= time.time():
            del self.items[item_id]
            return None
        return item

    def _check(self) -> None:
        if self.fail_with:
            raise self.fail_with

    async def read_item(self, item, partition_key):
        self._check()
        assert item == partition_key
        found = self._alive(item)
        if found is None:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")
        return {k: v for k, v in found.items() if k != "_created"}

    async def create_item(self, body):
        self._check()
        if self._alive(body["id"]) is not None:
            raise CosmosResourceExistsError(status_code=409, message="conflict")
        self.items[body["id"]] = {**body, "_etag": uuid.uuid4().hex, "_created": time.time()}
        return body

    async def replace_item(self, item, body, etag=None, match_condition=None):
        self._check()
        current = self._alive(item)
        if current is None:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")
        if etag is not None and current["_etag"] != etag:
            raise CosmosAccessConditionFailedError(status_code=412, message="precondition failed")
        self.items[item] = {**body, "_etag": uuid.uuid4().hex, "_created": time.time()}
        return body

    async def delete_item(self, item, partition_key, etag=None, match_condition=None):
        self._check()
        current = self._alive(item)
        if current is None:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")
        if etag is not None and current["_etag"] != etag:
            raise CosmosAccessConditionFailedError(status_code=412, message="precondition failed")
        del self.items[item]
