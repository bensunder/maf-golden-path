"""How a tool authenticates to a downstream API.

* :class:`ManagedIdentityAuth`: the service's own identity (app-level access).
* :class:`OnBehalfOfAuth`: the signed-in user's delegated access. The agent can never
  see more than the user can. No client secret: the service's managed identity is
  used as a federated credential on the app registration.
* :class:`ApiKeyAuth`: third-party APIs that only support keys (pull the key from Key Vault).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from agentkit.telemetry import get_run_context

__all__ = [
    "ApiKeyAuth",
    "AuthError",
    "BearerTokenAuth",
    "ManagedIdentityAuth",
    "NoAuth",
    "OnBehalfOfAuth",
    "ToolAuth",
]


class AuthError(RuntimeError):
    """Raised when a tool cannot authenticate; surfaced to the model as a short message."""


class ToolAuth(Protocol):
    async def headers(self) -> dict[str, str]: ...


class NoAuth:
    async def headers(self) -> dict[str, str]:
        return {}


class ApiKeyAuth:
    def __init__(self, key: str, header: str = "x-api-key") -> None:
        self._key, self._header = key, header

    async def headers(self) -> dict[str, str]:
        return {self._header: self._key}


class BearerTokenAuth:
    """Any async token source (for custom schemes and tests)."""

    def __init__(self, token_source: Callable[[], Awaitable[str]]) -> None:
        self._source = token_source

    async def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._source()}"}


class ManagedIdentityAuth:
    """App-level access with the service's managed identity (or az login locally).

    ``credential`` defaults to ``DefaultAzureCredential``; in the container,
    ``AZURE_CLIENT_ID`` (set by the template's Bicep) selects the user-assigned identity.
    """

    def __init__(self, scope: str, *, credential: Any = None) -> None:
        self._scope = scope
        self._credential = credential

    def _get_credential(self):
        if self._credential is None:
            from azure.identity.aio import DefaultAzureCredential

            self._credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        return self._credential

    async def headers(self) -> dict[str, str]:
        token = await self._get_credential().get_token(self._scope)
        return {"Authorization": f"Bearer {token.token}"}


CredentialFactory = Callable[[str], Any]


class OnBehalfOfAuth:
    """Delegated access for the user making the request (OAuth 2.0 on-behalf-of).

    The user's incoming token is taken from the request context (the agentkit host puts it
    there from the ``Authorization`` header). The service exchanges it for a token to
    ``scope`` using its app registration ``client_id``. By default, the client proves its
    identity with the managed identity as a federated credential, so no secret is stored.

    Setup (once per service): on the app registration ``client_id`` add a federated credential
    for the managed identity, and grant it delegated permission to the downstream API.
    """

    def __init__(
        self,
        scope: str,
        *,
        client_id: str | None = None,
        tenant_id: str | None = None,
        managed_identity_client_id: str | None = None,
        credential_factory: CredentialFactory | None = None,
    ) -> None:
        self._scope = scope
        self._client_id = client_id
        self._tenant_id = tenant_id
        self._mi_client_id = managed_identity_client_id
        self._factory = credential_factory or self._default_factory
        self._cache: dict[str, Any] = {}
        self._mi: Any = None

    def _default_factory(self, user_assertion: str):
        from azure.identity import ManagedIdentityCredential
        from azure.identity.aio import OnBehalfOfCredential

        if not (self._client_id and self._tenant_id):
            raise AuthError("OnBehalfOfAuth needs client_id and tenant_id")
        if self._mi is None:
            # The SDK calls client_assertion_func synchronously, so use the sync credential;
            # it caches the federated token, so the blocking call is rare and short.
            self._mi = ManagedIdentityCredential(client_id=self._mi_client_id)
        mi = self._mi
        return OnBehalfOfCredential(
            tenant_id=self._tenant_id,
            client_id=self._client_id,
            client_assertion_func=lambda: mi.get_token("api://AzureADTokenExchange/.default").token,
            user_assertion=user_assertion,
        )

    async def headers(self) -> dict[str, str]:
        ctx = get_run_context()
        assertion = ctx.user_assertion if ctx else None
        if not assertion:
            raise AuthError("this action needs the signed-in user's token, and none was provided")
        credential = self._cache.get(assertion)
        if credential is None:
            credential = self._factory(assertion)
            if len(self._cache) > 256:
                self._cache.clear()
            self._cache[assertion] = credential
        token = await credential.get_token(self._scope)
        return {"Authorization": f"Bearer {token.token}"}
