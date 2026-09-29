"""Per-agent Redis users (agentctl.py): against a real redis-server when one is installed."""

import asyncio
import importlib.util
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from agentkit.hosting import SessionRecord
from agentkit.hosting.sessions import RedisSessionStore

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("agentctl_redis", ROOT / "deploy" / "vps" / "agentctl.py")
ctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ctl)

DATA = {"agents": [{"name": "legal", "redis_password": "L" * 43}, {"name": "hr", "redis_password": "H" * 43}],
        "sample": {"redis_password": "S" * 43}}


@pytest.fixture(scope="module")
def redis_port(tmp_path_factory):
    if not shutil.which("redis-server"):
        pytest.skip("redis-server isn't installed")
    folder = tmp_path_factory.mktemp("redis")
    acl = folder / "users.acl"
    acl.write_text(ctl.redis_acl(DATA))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(["redis-server", "--port", str(port), "--bind", "127.0.0.1", "--save", "", "--appendonly", "no",
                             "--aclfile", str(acl), "--dir", str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield port
    proc.terminate()


def store(port, name, password):
    return RedisSessionStore.from_url(f"redis://{ctl.redis_user(name)}:{password}@127.0.0.1:{port}/0", prefix=f"agentkit:{name}:")


def test_the_acl_file_holds_hashes_not_passwords():
    text = ctl.redis_acl(DATA)
    assert "L" * 43 not in text and "user default off" in text
    assert "user agent-legal on #" in text and "~agentkit:legal:*" in text and "-@all" in text


def test_each_agent_uses_its_own_sessions_and_nothing_else(redis_port):
    from redis.asyncio import Redis
    from redis.exceptions import AuthenticationError, NoPermissionError, ResponseError

    async def flow():
        legal = store(redis_port, "legal", "L" * 43)
        record = SessionRecord(owner="dana@contoso.com", data={"x": 1}, expires_at=time.time() + 60, meta={})
        async with legal.lock("s1"):  # SET NX PX, then the EVAL release
            await legal.put("s1", record)
        assert (await legal.get("s1")).owner == "dana@contoso.com"

        hr_store = store(redis_port, "hr", "H" * 43)
        assert await hr_store.get("s1") is None  # its own prefix: it doesn't see legal's session

        as_legal = Redis.from_url(f"redis://agent-legal:{'L' * 43}@127.0.0.1:{redis_port}/0", decode_responses=True)
        with pytest.raises(NoPermissionError):
            await as_legal.get("agentkit:hr:session:s1")
        with pytest.raises(NoPermissionError):
            await as_legal.set("agentkit:hr:session:s1", '{"owner": "eve"}')
        for command in (("FLUSHALL",), ("KEYS", "*"), ("CONFIG", "GET", "*"), ("SELECT", "1")):
            with pytest.raises(NoPermissionError):
                await as_legal.execute_command(*command)
        with pytest.raises((AuthenticationError, ResponseError)):
            await Redis.from_url(f"redis://agent-legal:wrong@127.0.0.1:{redis_port}/0").ping()
        with pytest.raises((AuthenticationError, ResponseError, NoPermissionError)):
            await Redis.from_url(f"redis://127.0.0.1:{redis_port}/0").get("agentkit:legal:session:s1")  # default user is off
        await legal.delete("s1")
        assert await legal.get("s1") is None

    asyncio.run(flow())
