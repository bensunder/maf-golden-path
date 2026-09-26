"""The agentkit console at ``/console``: a read-only operations view of this agent service, plus a
playground that talks to the agent over AG-UI.

    app = create_app(create_agent, channels=[AgUiChannel(), WebChat(), Console()])

Everything the console shows comes from the running service, never from sample data:

* **Agent, tools, channels, knowledge**: the live MAF agent and the installed channels.
* **Security posture**: read from the agent's actual middleware stack and settings. A control that
  isn't in the stack is shown as off, whatever the docs say.
* **Evaluations**: the eval cases the image carries (``evals/cases.yaml``) and, if present, the last
  quality-gate report (``AGENTKIT_CONSOLE_EVAL_REPORT``, written by ``agentkit-gate --report``).
* **Deployment**: version, environment and hosting; commit and pipeline run when the deploy sets
  ``AGENTKIT_BUILD_COMMIT`` / ``AGENTKIT_BUILD_RUN_URL``.
* **Sessions and approvals**: the caller's own sessions by id (an approver sees only what is pending in
  someone else's), with the same ownership rules as the JSON API. Messages are never returned: only the
  arguments of pending approvals, and the owner's own audit trail.

The API needs the same sign-in as the rest of the service. ``role`` additionally restricts it to an
Entra app role (e.g. ``Console.Read``); the static page itself holds no data.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import time
from importlib import metadata, resources
from pathlib import Path
from typing import Any

import yaml
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from agentkit.hosting import AgentKitSettings, ConversationService, http_caller
from agentkit.hosting.approvals import APPROVALS_KEY, AUDIT_KEY, roles_from_principal

__all__ = ["Console", "security_posture"]

logger = logging.getLogger(__name__)

_CSP = ("default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
_SECURITY_HEADERS = {"Content-Security-Policy": _CSP, "X-Content-Type-Options": "nosniff",
                     "Referrer-Policy": "no-referrer"}
_API_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
_ASSET_TYPES = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".woff2": "font/woff2",
                ".png": "image/png", ".ico": "image/x-icon", ".json": "application/json"}


def _bundle() -> Any:
    return resources.files("agentkit.channels").joinpath("static", "console")


def _version(package: str) -> str | None:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None


# ---------------------------------------------------------------------------------- posture
def _control(cid: str, name: str, status: str, detail: str) -> dict[str, str]:
    return {"id": cid, "name": name, "status": status, "detail": detail}


def security_posture(agent: Any, settings: AgentKitSettings) -> list[dict[str, str]]:
    """The controls actually in force, from the agent's middleware stack and the service settings.

    ``status`` is ``on``, ``partial`` (on, with a gap worth knowing) or ``off``."""
    from agentkit.guardrails import (
        InputGuardMiddleware,
        PiiRedactionMiddleware,
        PromptShieldsDetector,
        SessionTokenBudgetMiddleware,
        ToolOutputShieldMiddleware,
        ToolPolicyMiddleware,
    )

    stack = list(getattr(agent, "middleware", None) or [])

    def find(kind: type) -> Any:
        return next((m for m in stack if isinstance(m, kind)), None)

    controls: list[dict[str, str]] = []
    hosted = _hosting()["platform"] != "Local"
    if settings.require_user and hosted:
        controls.append(_control("entra_auth", "Entra authentication", "on",
                                 "Calls without a signed-in identity are refused (401). The service trusts the identity "
                                 "headers, so it must sit behind Easy Auth or APIM; the generated Bicep puts it behind "
                                 "Easy Auth."))
    elif settings.require_user:
        controls.append(_control("entra_auth", "Entra authentication", "partial",
                                 "Calls without an identity are refused, but outside Azure nothing verifies the identity "
                                 "headers: anyone who can reach this port can claim to be anyone."))
    else:
        controls.append(_control("entra_auth", "Entra authentication", "off",
                                 "AGENTKIT_REQUIRE_USER is false: calls without an identity are accepted. Required in prod."))

    guard = find(InputGuardMiddleware)
    if guard is None:
        controls.append(_control("prompt_injection", "Prompt injection protection", "off",
                                 "No input guard in the middleware stack (AGENTKIT_GUARDRAIL_MODE=off)."))
    elif isinstance(getattr(guard, "_detector", None), PromptShieldsDetector):
        if settings.shields_fail_closed:
            controls.append(_control("prompt_injection", "Prompt injection protection", "on",
                                     "Azure AI Content Safety Prompt Shields checks every input before the model, and "
                                     "refuses the input if Prompt Shields can't be reached (fails closed)."))
        else:
            controls.append(_control("prompt_injection", "Prompt injection protection", "partial",
                                     "Prompt Shields checks every input, but inputs pass unchecked when it can't be "
                                     "reached (AGENTKIT_SHIELDS_FAIL_CLOSED=false)."))
    else:
        controls.append(_control("prompt_injection", "Prompt injection protection", "partial",
                                 "Local heuristic detector. Prod requires Prompt Shields."))

    shield = find(ToolOutputShieldMiddleware)
    controls.append(_control("tool_output", "Tool output scanning", "on" if shield else "off",
                             "Tool results are scanned as documents before the model reads them." if shield
                             else "Tool results reach the model unscanned (AGENTKIT_SCAN_TOOL_OUTPUT=false or no detector)."))

    pii = find(PiiRedactionMiddleware)
    kinds = ", ".join(k.lower() if k != "SSN" else "SSN" for k in getattr(pii, "_kinds", ())) if pii else ""
    controls.append(_control("pii", "PII redaction", "on" if pii else "off",
                             f"Redacted from user messages before the model sees them: {kinds}."
                             if pii else "AGENTKIT_REDACT_PII is false."))

    policy = find(ToolPolicyMiddleware)
    if policy is not None:
        denied = len(getattr(policy, "_denied", ()) or ())
        validators = len(getattr(policy, "_validators", {}) or {})
        allowed = getattr(policy, "_allowed", None)
        parts = [f"{validators} argument validator{'s' if validators != 1 else ''}", f"{denied} denied"]
        if allowed is not None:
            parts.append(f"allow-list of {len(allowed)}")
        controls.append(_control("tool_policy", "Tool policy enforcement", "on", ", ".join(parts) + "."))
    else:
        controls.append(_control("tool_policy", "Tool policy enforcement", "off", "No tool policy configured."))

    tools = describe_tools(agent)
    gated = [t["name"] for t in tools if t["approval"] != "never"]
    if gated:
        verb = "waits" if len(gated) == 1 else "wait"
        rules = " unless an auto-approval rule matches (for example, small amounts)" if any(
            t["approval"] == "rules" for t in tools) else ""
        if settings.approver_role:
            who = f"an approver with the '{settings.approver_role}' role" + (
                ", never the requester" if settings.approval_separation else "")
            controls.append(_control("human_approval", "Human approval", "on",
                                     f"{', '.join(gated)} {verb} for {who}{rules}."))
        else:
            controls.append(_control("human_approval", "Human approval", "partial",
                                     f"{', '.join(gated)} {verb} for the requesting user to confirm{rules}. Set "
                                     "AGENTKIT_APPROVER_ROLE so someone else approves (separation of duties)."))
    else:
        controls.append(_control("human_approval", "Human approval", "off", "No tool requires approval."))

    if settings.require_user:
        controls.append(_control("session_isolation", "Session isolation", "on",
                                 "Each session belongs to the user who started it; others get 403."))
    else:
        controls.append(_control("session_isolation", "Session isolation", "partial",
                                 "Sessions are bound to their user, but anonymous callers share one identity."))

    budget = find(SessionTokenBudgetMiddleware)
    controls.append(_control("token_budget", "Token budget", "on" if budget else "off",
                             f"{getattr(budget, '_max', settings.session_token_budget):,} tokens per session; "
                             f"at most {settings.max_iterations} model calls and {settings.max_function_calls} "
                             f"tool calls per run." if budget else "No per-session token budget."))

    exporter = bool(settings.appinsights_connection_string or settings.otlp_endpoint)
    controls.append(_control("audit", "Audit logging", "on" if exporter else "partial",
                             "Approval decisions are kept with the session; runs, users and decisions are traced to "
                             + ("Application Insights." if settings.appinsights_connection_string else "OTLP.")
                             if exporter else
                             "Approval decisions are kept with the session, but no telemetry exporter is configured."))
    controls.append(_control("content_capture", "No prompt content in telemetry",
                             "off" if settings.capture_message_content else "on",
                             "Prompts and replies are recorded in traces (AGENTKIT_CAPTURE_MESSAGE_CONTENT=true)."
                             if settings.capture_message_content else "Traces carry metadata only, not what users typed."))
    return controls


def describe_tools(agent: Any) -> list[dict[str, Any]]:
    tools = (getattr(agent, "default_options", None) or {}).get("tools") or []
    rules = any(type(m).__name__ == "ToolApprovalMiddleware" and getattr(m, "auto_approval_rules", None)
                for m in getattr(agent, "middleware", None) or [])
    result = []
    for t in tools:
        name = getattr(t, "name", None)
        if not name:
            continue
        mode = getattr(t, "approval_mode", None)
        approval = "never"
        if mode == "always_require":
            approval = "rules" if rules else "always"
        props = getattr(t, "additional_properties", None) or {}
        result.append({"name": name, "description": (getattr(t, "description", "") or "")[:300], "approval": approval,
                       "kind": "knowledge" if "agentkit.knowledge" in props else "function"})
    return result


def _knowledge(agent: Any) -> dict[str, Any] | None:
    for t in (getattr(agent, "default_options", None) or {}).get("tools") or []:
        props = getattr(t, "additional_properties", None) or {}
        if "agentkit.knowledge" in props:
            return {"tool": getattr(t, "name", None), **props["agentkit.knowledge"]}
    return None


_CHANNEL_NAMES = {
    "AgUiChannel": ("agui", "AG-UI", "path"),
    "WebChat": ("web_chat", "Web chat", "path"),
    "TeamsChannel": ("teams", "Microsoft Teams", "path"),
}


def _channels(app: Any, console_path: str) -> list[dict[str, Any]]:
    result = [{"id": "api", "name": "JSON API", "path": "/v1/chat"}]
    for channel in getattr(app.state, "channels", []):
        kind = type(channel).__name__
        if kind == "Console":
            continue
        cid, name, attr = _CHANNEL_NAMES.get(kind, (kind.lower(), kind, "path"))
        result.append({"id": cid, "name": name, "path": getattr(channel, attr, None) or
                       ("/api/messages" if cid == "teams" else None)})
    return result


def _hosting() -> dict[str, Any]:
    if os.getenv("CONTAINER_APP_NAME"):
        return {"platform": "Azure Container Apps", "app": os.getenv("CONTAINER_APP_NAME"),
                "revision": os.getenv("CONTAINER_APP_REVISION"), "replica": os.getenv("CONTAINER_APP_REPLICA_NAME")}
    if os.getenv("WEBSITE_SITE_NAME"):
        return {"platform": "Azure App Service", "app": os.getenv("WEBSITE_SITE_NAME"),
                "revision": None, "replica": os.getenv("WEBSITE_INSTANCE_ID", "")[:12] or None}
    return {"platform": "Local", "app": None, "revision": None, "replica": None}


# ---------------------------------------------------------------------------------- evals
def _load_cases(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cases = []
    for raw in data.get("cases", []):
        expect = raw.get("expect") or {}
        checks = []
        for key, label in (("contains", "Contains"), ("not_contains", "Excludes"), ("tools", "Tools called"),
                           ("forbidden_tools", "Forbidden tools"), ("tool_args", "Tool arguments"),
                           ("max_tool_calls", "Tool-call limit"), ("blocked", "Refused"), ("approval", "Approval"),
                           ("cites", "Citations"), ("must_not_retrieve", "Permission trimming"),
                           ("rubric", "Rubric (judge)"), ("grounded", "Groundedness (judge)")):
            if expect.get(key) not in (None, False, [], {}):
                checks.append(label)
        cases.append({"id": str(raw.get("id")), "input": str(raw.get("input", ""))[:300],
                      "critical": bool(raw.get("critical")), "as_user": bool(raw.get("user")),
                      "scripted": bool(raw.get("script")), "checks": checks})
    return cases


class Console:
    def __init__(self, path: str = "/console", *, api: str = "/v1/console", title: str | None = None,
                 role: str | None = None, eval_cases: str | Path | None = None,
                 eval_report: str | Path | None = None, workbook_url: str | None = None,
                 docs_url: str | None = "https://github.com/bensunder/maf-golden-path/tree/main/docs"):
        """``role``: Entra app role required for the console API (default ``AGENTKIT_CONSOLE_ROLE``; empty =
        any signed-in user of the service). ``eval_cases`` defaults to ``evals/cases.yaml`` in the working
        directory, ``eval_report`` to ``AGENTKIT_CONSOLE_EVAL_REPORT`` or else ``evals/gate-report.json`` when that
        file exists (the deploy pipeline writes it into the image). ``workbook_url`` (or
        ``AGENTKIT_CONSOLE_WORKBOOK_URL``) links the telemetry page to the platform's operations workbook."""
        self.path = path.rstrip("/")
        self.api = api.rstrip("/")
        self.title = title
        self.role = role if role is not None else (os.getenv("AGENTKIT_CONSOLE_ROLE") or None)
        self.eval_cases = Path(eval_cases or os.getenv("AGENTKIT_CONSOLE_EVAL_CASES") or "evals/cases.yaml")
        report = eval_report or os.getenv("AGENTKIT_CONSOLE_EVAL_REPORT")
        self.eval_report = Path(report) if report else None
        self._default_report = Path("evals/gate-report.json")
        self.workbook_url = workbook_url or os.getenv("AGENTKIT_CONSOLE_WORKBOOK_URL") or None
        self.docs_url = docs_url
        self.started_at = time.time()

    # ------------------------------------------------------------ install
    def install(self, app: Any, service: ConversationService, settings: AgentKitSettings) -> None:
        self._app, self._service, self._settings = app, service, settings

        index = _bundle().joinpath("index.html")
        if not index.is_file():  # pragma: no cover - the wheel always carries the build
            raise RuntimeError("agentkit console bundle is missing (run `npm run build` in console/)")
        page = (index.read_text(encoding="utf-8")
                .replace("/console/", f"{self.path}/")
                .replace("__AGENTKIT_API__", _html_attr(self.api))
                .replace("__AGENTKIT_TITLE__", _html_attr(self.title or settings.service_name)))

        async def console_page() -> HTMLResponse:
            return HTMLResponse(page, headers={**_SECURITY_HEADERS, "Cache-Control": "no-cache"})

        async def console_asset(name: str) -> Response:
            if "/" in name or "\\" in name or name.startswith("."):
                raise HTTPException(404)
            item = _bundle().joinpath("assets", name)
            if not item.is_file():
                raise HTTPException(404)
            suffix = Path(name).suffix
            media = _ASSET_TYPES.get(suffix) or mimetypes.guess_type(name)[0] or "application/octet-stream"
            body = item.read_bytes()
            if suffix == ".css" and self.path != "/console":  # font URLs are absolute in the stylesheet
                body = body.replace(b"/console/assets/", f"{self.path}/assets/".encode())
            return Response(body, media_type=media,
                            headers={**_SECURITY_HEADERS, "Cache-Control": "public, max-age=31536000, immutable"})

        async def console_route(rest: str) -> HTMLResponse:  # client-side routes: /console/agents, ...
            if rest.startswith("assets/") or "." in rest.rsplit("/", 1)[-1]:
                raise HTTPException(404)  # a missing file is a 404, not the app shell
            return await console_page()

        app.add_api_route(self.path, console_page, methods=["GET"], include_in_schema=False)
        app.add_api_route(f"{self.path}/assets/{{name}}", console_asset, methods=["GET"], include_in_schema=False)
        app.add_api_route(f"{self.path}/{{rest:path}}", console_route, methods=["GET"], include_in_schema=False)

        app.add_api_route(f"{self.api}/overview", self._overview, methods=["GET"], tags=["Console"],
                          summary="This service: agent, tools, channels, security posture, deployment")
        app.add_api_route(f"{self.api}/evals", self._evals, methods=["GET"], tags=["Console"],
                          summary="Eval cases and the last quality-gate report")
        app.add_api_route(f"{self.api}/sessions/{{session_id}}", self._session, methods=["GET"], tags=["Console"],
                          summary="Metadata of one of your sessions: expiry, pending approvals, audit trail")

    # ------------------------------------------------------------ access
    def _caller(self, request: Request):
        caller = http_caller(request, self._settings, channel="console")
        if self.role and self.role not in roles_from_principal(request.headers.get(self._settings.principal_claims_header)):
            raise HTTPException(403, f"the console requires the '{self.role}' role")
        return caller

    # ------------------------------------------------------------ endpoints
    async def _overview(self, request: Request) -> JSONResponse:
        caller = self._caller(request)
        s = self._settings
        try:
            agent = self._service.agent
        except HTTPException:
            raise HTTPException(503, "agent not initialised") from None
        exporter = "app_insights" if s.appinsights_connection_string else ("otlp" if s.otlp_endpoint else None)
        body = {
            "service": {
                "name": s.service_name, "title": self.title or s.service_name, "version": s.service_version,
                "environment": s.environment, "team": s.team, "kit_version": _version("agentkit-hosting"),
                "maf_version": _version("agent-framework-core") or _version("agent-framework"),
                "commit": os.getenv("AGENTKIT_BUILD_COMMIT") or None,
                "run_url": os.getenv("AGENTKIT_BUILD_RUN_URL") or None,
                "deployed_at": os.getenv("AGENTKIT_BUILD_TIME") or None,
                "started_at": self.started_at, "hosting": _hosting(),
            },
            "caller": {"user": caller.user_id, "is_approver": caller.is_approver},
            "agent": {
                "name": agent.name, "description": agent.description, "model": s.model,
                "gateway": bool(s.gateway_endpoint), "auth_mode": s.auth_mode,
                "tools": describe_tools(agent),
                "limits": {"max_iterations": s.max_iterations, "max_function_calls": s.max_function_calls,
                           "max_run_seconds": s.max_run_seconds, "session_token_budget": s.session_token_budget,
                           "session_ttl_seconds": s.session_ttl_seconds, "max_input_chars": s.max_input_chars},
            },
            "channels": _channels(self._app, self.path),
            "knowledge": _knowledge(agent),
            "security": security_posture(agent, s),
            "sessions": {"store": s.session_store, "shared": s.session_store != "memory",
                         "ttl_seconds": s.session_ttl_seconds},
            "approvals": {"mode": "separation" if s.approver_role and s.approval_separation
                          else ("approver" if s.approver_role else "confirmation"),
                          "approver_role": s.approver_role},
            "telemetry": {"exporter": exporter, "capture_content": s.capture_message_content,
                          "workbook_url": self.workbook_url},
            "links": {"docs": self.docs_url, "chat": _path_of(self._app, "WebChat")},
        }
        return JSONResponse(body, headers=_API_HEADERS)

    async def _evals(self, request: Request) -> JSONResponse:
        self._caller(request)
        cases, cases_error, report, report_error = None, None, None, None
        if self.eval_cases.is_file():
            try:
                cases = _load_cases(self.eval_cases)
            except Exception as exc:  # a broken file is shown as such, not as "no cases"
                logger.warning("console: could not read %s: %s", self.eval_cases, exc)
                cases_error = "The eval cases file could not be read."
        report_path = self.eval_report or (self._default_report if self._default_report.is_file() else None)
        if report_path:
            if report_path.is_file():
                try:
                    raw = json.loads(report_path.read_text(encoding="utf-8"))
                    report = {k: raw.get(k) for k in ("passed", "reasons", "live", "repeat", "pass_rate",
                                                      "started_at", "duration_s", "baseline_used")}
                    report["cases"] = [{k: c.get(k) for k in ("id", "critical", "runs", "passed_runs", "pass_rate",
                                                              "scores", "failures", "skipped", "mean_duration_s")}
                                       for c in raw.get("cases") or []]
                except (ValueError, AttributeError, TypeError) as exc:
                    logger.warning("console: could not read %s: %s", report_path, exc)
                    report_error = "The quality-gate report could not be read."
            else:
                report_error = "The configured quality-gate report file was not found."
        return JSONResponse({"cases": cases, "cases_error": cases_error, "report": report,
                             "report_error": report_error}, headers=_API_HEADERS)

    async def _session(self, session_id: str, request: Request) -> JSONResponse:
        caller = self._caller(request)
        record = await self._service.store.get(session_id)
        if record is None:
            raise HTTPException(404, "session not found or expired")
        owner = record.owner == caller.user_id
        approver = bool(self._settings.approver_role) and caller.is_approver
        if not owner and not approver:
            raise HTTPException(403, "session belongs to another user")
        pending = record.meta.get(APPROVALS_KEY) or []
        # The audit trail (arguments, comments, who decided) is the owner's. An approver sees what's pending,
        # exactly as the JSON API shows them.
        audit = (record.meta.get(AUDIT_KEY) or []) if owner else []
        body = {
            "id": session_id, "yours": owner, "expires_at": record.expires_at,
            "channel": "agui" if session_id.startswith("agui-") else ("teams" if record.meta.get("teams.conversation") else None),
            "pending": [{"id": p["id"], "tool": p["tool"], "arguments": p["arguments"],
                         "requested_at": p.get("requested_at")} for p in pending],
            "audit": [{k: a.get(k) for k in ("id", "tool", "arguments", "approved", "decided_by", "decided_by_name",
                                             "channel", "comment", "requested_by", "decided_at")} for a in audit],
        }
        return JSONResponse(body, headers=_API_HEADERS)


def _path_of(app: Any, kind: str) -> str | None:
    for channel in getattr(app.state, "channels", []):
        if type(channel).__name__ == kind:
            return getattr(channel, "path", None)
    return None


def _html_attr(value: str) -> str:
    import html

    return html.escape(value, quote=True)
