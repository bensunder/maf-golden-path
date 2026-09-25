"""Model clients bound to the AI gateway, with Entra ID auth and run caps."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from agent_framework.openai import OpenAIChatCompletionClient
from openai import AsyncAzureOpenAI, AsyncOpenAI

from .settings import AgentKitSettings

__all__ = ["create_chat_client", "get_credential", "token_provider"]


def get_credential(settings: AgentKitSettings):
    """Async Entra credential for the configured auth mode (None for api_key)."""
    from azure.identity.aio import AzureCliCredential, DefaultAzureCredential, ManagedIdentityCredential

    if settings.auth_mode == "api_key":
        return None
    if settings.auth_mode == "managed_identity":
        return ManagedIdentityCredential(client_id=settings.managed_identity_client_id)
    if settings.auth_mode == "azure_cli":
        return AzureCliCredential()
    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


def token_provider(settings: AgentKitSettings, credential: Any = None) -> Callable[[], Awaitable[str]] | None:
    if settings.auth_mode == "api_key":
        return None
    from azure.identity.aio import get_bearer_token_provider

    return get_bearer_token_provider(credential or get_credential(settings), settings.token_scope)


def gateway_headers(settings: AgentKitSettings, agent_name: str) -> dict[str, str]:
    """Headers APIM policies key on (per-team token quotas, chargeback, tracing)."""
    headers = {
        "x-agentkit-agent": agent_name,
        "x-agentkit-team": settings.team,
        "x-agentkit-service": settings.service_name,
        "x-agentkit-env": settings.environment,
    }
    if settings.gateway_subscription_key:
        headers["Ocp-Apim-Subscription-Key"] = settings.gateway_subscription_key.get_secret_value()
    return headers


def create_chat_client(
    settings: AgentKitSettings,
    *,
    agent_name: str,
    azure_ad_token_provider: Callable[[], Awaitable[str] | str] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> OpenAIChatCompletionClient:
    """Chat Completions client pointed at the gateway.

    Chat Completions is the default because every APIM GenAI policy (token limit,
    semantic cache, content safety) supports it. The OpenAI SDK client is built here,
    not by MAF, so auth, headers and transport are controlled in one place.
    """
    if not settings.gateway_endpoint:
        raise ValueError("AGENTKIT_GATEWAY_ENDPOINT is not set")
    headers = gateway_headers(settings, agent_name)
    api_key = settings.api_key.get_secret_value() if settings.api_key else None
    if settings.auth_mode != "api_key" and azure_ad_token_provider is None:
        azure_ad_token_provider = token_provider(settings)

    common: dict[str, Any] = {"default_headers": headers, "max_retries": 3, "timeout": settings.max_run_seconds}
    if http_client is not None:
        common["http_client"] = http_client

    if settings.gateway_style == "azure":
        sdk_client: AsyncOpenAI = AsyncAzureOpenAI(
            azure_endpoint=settings.gateway_endpoint,
            azure_deployment=settings.model,
            api_version=settings.api_version,
            api_key=api_key,
            azure_ad_token_provider=azure_ad_token_provider if api_key is None else None,
            **common,
        )
    else:
        if api_key is None:
            # OpenAI-compatible v1 endpoint with Entra: the SDK accepts a callable api_key.
            api_key = azure_ad_token_provider  # type: ignore[assignment]
        sdk_client = AsyncOpenAI(base_url=settings.gateway_endpoint.rstrip("/") + "/openai/v1", api_key=api_key, **common)

    return OpenAIChatCompletionClient(
        model=settings.model,
        async_client=sdk_client,
        function_invocation_configuration={
            "max_iterations": settings.max_iterations,
            "max_function_calls": settings.max_function_calls,
            "max_duration_seconds": settings.max_run_seconds,
        },
    )
