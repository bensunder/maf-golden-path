"""All knobs an agent service has, read from ``AGENTKIT_*`` environment variables."""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["AgentKitSettings"]


class AgentKitSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTKIT_", env_file=".env", extra="ignore")

    # identity of the service
    environment: Literal["local", "dev", "test", "prod"] = "local"
    service_name: str = "agent"
    service_version: str = "0.0.0"
    team: str = "unassigned"

    # model access — always through the AI gateway (APIM) outside local dev
    gateway_endpoint: str | None = Field(
        default=None, description="AI gateway base URL, e.g. https://apim-ai.contoso.com"
    )
    gateway_style: Literal["azure", "openai_v1"] = Field(
        default="azure",
        description="'azure' = /openai/deployments/{model}/... ; 'openai_v1' = OpenAI-compatible /openai/v1",
    )
    model: str = "gpt-4.1-mini"
    api_version: str = "2024-10-21"
    auth_mode: Literal["managed_identity", "azure_cli", "default", "api_key"] = "default"
    managed_identity_client_id: str | None = None
    token_scope: str = "https://cognitiveservices.azure.com/.default"
    api_key: SecretStr | None = None
    gateway_subscription_key: SecretStr | None = Field(
        default=None, description="APIM subscription key (per-team product) for quota and chargeback"
    )

    # run limits (MAF FunctionInvocationConfiguration + session budget)
    max_iterations: int = 8
    max_function_calls: int = 20
    max_run_seconds: float = 120.0
    session_token_budget: int = 200_000
    session_ttl_seconds: int = 3_600
    max_input_chars: int = 20_000

    # guardrails
    guardrail_mode: Literal["prompt_shields", "heuristic", "off"] = "heuristic"
    content_safety_endpoint: str | None = None
    content_safety_key: SecretStr | None = None
    shields_fail_closed: bool = True
    redact_pii: bool = True
    scan_tool_output: bool = True

    # telemetry
    otlp_endpoint: str | None = None
    appinsights_connection_string: str | None = Field(
        default=None,
        validation_alias=AliasChoices("AGENTKIT_APPINSIGHTS_CONNECTION_STRING", "APPLICATIONINSIGHTS_CONNECTION_STRING"),
    )
    capture_message_content: bool = False

    # HTTP hosting: identity comes from platform auth (Container Apps / App Service Easy Auth, APIM)
    user_header: str = "x-ms-client-principal-name"
    tenant_header: str = "x-agentkit-tenant"
    require_user: bool = False
    #: Header carrying the caller's own access token, made available to on-behalf-of tools
    #: (agentkit.tools.OnBehalfOfAuth). Easy Auth validates it before it reaches the app.
    user_token_header: str = "authorization"

    # sessions (shared stores allow more than one replica)
    session_store: Literal["memory", "redis", "cosmos"] = "memory"
    redis_url: SecretStr | None = None
    cosmos_endpoint: str | None = None
    cosmos_database: str = "agentkit"
    cosmos_container: str | None = None

    # human approvals
    #: Entra app role an approver must hold. Empty = the requesting user confirms their own actions.
    approver_role: str | None = None
    #: With an approver role, forbid approving your own request (separation of duties).
    approval_separation: bool = True
    #: Easy Auth header with the caller's claims (base64 JSON), used to read app roles.
    principal_claims_header: str = "x-ms-client-principal"

    @model_validator(mode="after")
    def _enforce_environment_policy(self) -> AgentKitSettings:
        problems: list[str] = []
        if self.environment == "prod":
            if self.auth_mode not in ("managed_identity",):
                problems.append("auth_mode must be 'managed_identity' in prod")
            if not self.gateway_endpoint:
                problems.append("gateway_endpoint is required in prod (all model traffic goes through the AI gateway)")
            if self.guardrail_mode != "prompt_shields":
                problems.append("guardrail_mode must be 'prompt_shields' in prod")
            if self.capture_message_content:
                problems.append("capture_message_content must be false in prod")
            if not self.require_user:
                problems.append("require_user must be true in prod")
        if self.guardrail_mode == "prompt_shields" and not self.content_safety_endpoint:
            problems.append("guardrail_mode 'prompt_shields' needs content_safety_endpoint")
        if self.auth_mode == "api_key" and not self.api_key:
            problems.append("auth_mode 'api_key' needs api_key")
        if self.session_store == "redis" and not self.redis_url:
            problems.append("session_store 'redis' needs redis_url")
        if self.session_store == "cosmos" and not (self.cosmos_endpoint and self.cosmos_container):
            problems.append("session_store 'cosmos' needs cosmos_endpoint and cosmos_container")
        if problems:
            raise ValueError("; ".join(problems))
        return self
