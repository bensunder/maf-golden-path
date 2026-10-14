"""Settings, the prod policy, and one shared service per configuration."""

import pytest

from agentkit.hosting import AgentKitSettings
from agentkit.memory import (
    CosmosMemoryStore,
    InMemoryMemoryStore,
    MemorySettings,
    RedisMemoryStore,
    memory_service,
)

GOOD_SALT = "a-deployment-secret-0123456789"


def kit(**kw):
    kw.setdefault("environment", "test")
    return AgentKitSettings(guardrail_mode="heuristic", _env_file=None, **kw)


def mem(**kw):
    return MemorySettings(_env_file=None, **kw)


def test_defaults_inherit_the_session_store_and_share_one_instance():
    a = memory_service(kit(), mem())
    b = memory_service(kit(), mem())
    assert a is b and isinstance(a.store, InMemoryMemoryStore)
    assert a.config["store"] == "memory" and a.config["salt_configured"] is False


def test_different_configuration_builds_a_different_service():
    assert memory_service(kit(), mem()) is not memory_service(kit(), mem(retention_days=30))


def test_redis_inherits_url_and_agent_prefix(redis_url):
    svc = memory_service(kit(session_store="redis", redis_url=redis_url, session_key_prefix="agentkit:payments:"),
                         mem(salt=GOOD_SALT))
    assert isinstance(svc.store, RedisMemoryStore) and svc.store._key("u") == "agentkit:payments:memory:u"
    assert svc.config["salt_configured"] is True


def test_cosmos_uses_the_session_container_by_default():
    class Container:
        pass

    class Db:
        def get_container_client(self, name):
            Container.name = name
            return Container()

    class Client:
        def __init__(self, endpoint, credential):
            pass

        def get_database_client(self, name):
            return Db()

    import azure.cosmos.aio as aio

    original = aio.CosmosClient
    aio.CosmosClient = Client
    try:
        svc = memory_service(kit(session_store="cosmos", cosmos_endpoint="https://x.documents.azure.com",
                                 cosmos_container="sessions"), mem(salt=GOOD_SALT), credential=object())
    finally:
        aio.CosmosClient = original
    assert isinstance(svc.store, CosmosMemoryStore) and Container.name == "sessions"


@pytest.mark.parametrize("memory_kw,match", [
    ({}, "SALT is required in prod"),
    ({"salt": GOOD_SALT, "store": "memory"}, "in-process memory store"),
    ({"salt": GOOD_SALT, "store": "redis", "dev_user": "me"}, "local and test only"),
])
def test_prod_policy(memory_kw, match):
    prod = AgentKitSettings.model_construct(**{**kit(session_store="redis", redis_url="redis://x:1/0").model_dump(),
                                              "environment": "prod"})
    with pytest.raises(ValueError, match=match):
        memory_service(prod, mem(**memory_kw))


def test_short_salt_and_missing_backends_are_refused():
    with pytest.raises(ValueError, match="at least 16"):
        memory_service(kit(), mem(salt="short"))
    with pytest.raises(ValueError, match="needs AGENTKIT_REDIS_URL"):
        memory_service(kit(), mem(store="redis"))
    with pytest.raises(ValueError, match="cosmos memory store needs"):
        memory_service(kit(), mem(store="cosmos"))


def test_dev_user_only_outside_prod_and_dev():
    assert memory_service(kit(environment="local"), mem(dev_user="me")).dev_user == "me"
    assert memory_service(kit(environment="dev"), mem(dev_user="me")).dev_user is None


def test_env_variables_are_read(monkeypatch):
    monkeypatch.setenv("AGENTKIT_MEMORY_RETENTION_DAYS", "30")
    monkeypatch.setenv("AGENTKIT_MEMORY_MAX_ITEMS", "10")
    s = MemorySettings(_env_file=None)
    assert (s.retention_days, s.max_items) == (30, 10)
    with pytest.raises(ValueError):
        MemorySettings(_env_file=None, retention_days=0)
