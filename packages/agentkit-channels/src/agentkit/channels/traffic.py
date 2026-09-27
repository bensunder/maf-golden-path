"""Live traffic from Azure Monitor for the console and the fleet view: runs, errors, latency, tool calls
and model tokens over time, from the same telemetry the platform's dashboard reads.

The service queries Log Analytics with its own managed identity, scoped to the platform's Application
Insights resource (the template grants ``Monitoring Reader`` on it). Nothing is stored here: every
number is a query result, cached for a minute.

    AGENTKIT_CONSOLE_LOGS_RESOURCE=/subscriptions/…/components/agentkit-appi-…   # App Insights resource id
    # or AGENTKIT_CONSOLE_LOGS_WORKSPACE_ID=<Log Analytics workspace customer id>
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

__all__ = ["RANGES", "AzureLogsClient", "LogsClient", "LogsQueryError", "TrafficQueries", "kql_string"]

logger = logging.getLogger(__name__)

#: range -> (timespan, bin): the bin keeps every chart around 12-28 points.
RANGES: dict[str, tuple[str, str, int]] = {
    "1h": ("PT1H", "5m", 5),
    "24h": ("PT24H", "1h", 60),
    "7d": ("P7D", "6h", 360),
}


class LogsQueryError(Exception):
    """The query couldn't run (permissions, workspace, network). The message is safe to show."""


class LogsClient(Protocol):
    async def query(self, kql: str, timespan: str) -> list[dict[str, Any]]: ...


def kql_string(value: str) -> str:
    """A KQL string literal (values come from settings and the registry, but are escaped anyway)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


class AzureLogsClient:
    """Log Analytics query API with an Entra token. ``scope`` is an Azure resource id (resource-centric
    query, e.g. the Application Insights component) or ``/workspaces/<customer id>``."""

    def __init__(self, scope: str, token: Callable[[], Awaitable[str]], *,
                 endpoint: str = "https://api.loganalytics.io/v1", timeout: float = 30.0,
                 client: httpx.AsyncClient | None = None):
        self.scope = "/" + scope.strip("/")
        self.token = token
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self._client = client

    @classmethod
    def for_resource(cls, resource_id: str, credential: Any, **kwargs: Any) -> AzureLogsClient:
        return cls(resource_id, _bearer(credential), **kwargs)

    @classmethod
    def for_workspace(cls, workspace_id: str, credential: Any, **kwargs: Any) -> AzureLogsClient:
        return cls(f"/workspaces/{workspace_id}", _bearer(credential), **kwargs)

    async def query(self, kql: str, timespan: str) -> list[dict[str, Any]]:
        try:
            headers = {"Authorization": f"Bearer {await self.token()}"}
        except Exception as exc:  # no identity, or it can't get a token
            logger.warning("logs query: no token: %s", exc)
            raise LogsQueryError("This service couldn't get a token for Azure Monitor.") from None
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        try:
            response = await client.post(f"{self.endpoint}{self.scope}/query", headers=headers,
                                         json={"query": kql, "timespan": timespan})
        except httpx.HTTPError as exc:
            logger.warning("logs query failed: %s", exc)
            raise LogsQueryError("Azure Monitor couldn't be reached.") from None
        finally:
            if self._client is None:
                await client.aclose()
        if response.status_code in (401, 403):
            raise LogsQueryError("This service isn't allowed to read the telemetry (it needs Monitoring Reader "
                                 "on the Application Insights resource).")
        if response.status_code != 200:
            logger.warning("logs query HTTP %s: %s", response.status_code, response.text[:500])
            raise LogsQueryError(f"Azure Monitor answered HTTP {response.status_code}.")
        body = response.json()
        if body.get("error"):  # a partial result: showing it would under-count without saying so
            logger.warning("logs query partial result: %s", str(body["error"])[:500])
            raise LogsQueryError("Azure Monitor returned an incomplete result; try a shorter time range.")
        return rows(body)


def rows(body: dict[str, Any]) -> list[dict[str, Any]]:
    tables = body.get("tables") or []
    if not tables:
        return []
    columns = [c["name"] for c in tables[0].get("columns", [])]
    return [dict(zip(columns, row)) for row in tables[0].get("rows", [])]


def _bearer(credential: Any) -> Callable[[], Awaitable[str]]:
    async def token() -> str:
        result = await credential.get_token("https://api.loganalytics.io/.default")
        return result.token
    return token


# ------------------------------------------------------------------------------------ queries
@dataclass(frozen=True)
class TrafficQueries:
    """KQL over the telemetry every agentkit service emits (see docs/operations.md, metrics reference).

    Agent metrics carry ``gen_ai.agent.name``; spans carry the service as ``AppRoleName``; gateway token
    metrics carry the ``Agent`` and ``Environment`` dimensions."""

    @staticmethod
    def series(agent: str, service: str, environment: str, bin_: str, window: str) -> str:
        """Agent metrics are scoped to this service (``AppRoleName``) *and* agent, so another service with an
        agent of the same name, or a developer's laptop exporting to the same workspace, can't inflate them.
        Gateway tokens carry only agent and environment."""
        a, s, e = kql_string(agent), kql_string(service), kql_string(environment)
        return f"""
let runs = AppMetrics
| where TimeGenerated > ago({window}) and AppRoleName == {s}
| where Name in ("agentkit.agent.runs", "agentkit.agent.run.duration") and tostring(Properties["gen_ai.agent.name"]) == {a}
| summarize Runs = sumif(Sum, Name == "agentkit.agent.runs"),
            Errors = sumif(Sum, Name == "agentkit.agent.runs" and tostring(Properties["outcome"]) == "error"),
            Blocked = sumif(Sum, Name == "agentkit.agent.runs" and tostring(Properties["outcome"]) == "blocked"),
            DurationSum = sumif(Sum, Name == "agentkit.agent.run.duration"),
            DurationCount = sumif(ItemCount, Name == "agentkit.agent.run.duration"),
            DurationMax = maxif(Max, Name == "agentkit.agent.run.duration")
  by T = bin(TimeGenerated, {bin_});
let tokens = AppMetrics
| where TimeGenerated > ago({window})
| where Name == "Total Tokens" and tostring(Properties["Agent"]) == {a} and tostring(Properties["Environment"]) == {e}
| summarize Tokens = sum(Sum) by T = bin(TimeGenerated, {bin_});
runs | join kind=fullouter tokens on T
| project T = coalesce(T, T1), Runs = coalesce(todouble(Runs), 0.0), Errors = coalesce(todouble(Errors), 0.0),
          Blocked = coalesce(todouble(Blocked), 0.0),
          DurationSum = coalesce(todouble(DurationSum), 0.0), DurationCount = coalesce(todouble(DurationCount), 0.0),
          DurationMax = DurationMax, Tokens = coalesce(todouble(Tokens), 0.0)
| order by T asc""".strip()

    @staticmethod
    def tools(service: str, window: str) -> str:
        s = kql_string(service)
        return f"""
let calls = AppDependencies
| where TimeGenerated > ago({window}) and AppRoleName == {s} and Name startswith "execute_tool"
| extend Tool = coalesce(tostring(Properties["gen_ai.tool.name"]), trim_start("execute_tool ", Name));
calls
| summarize Calls = count(), Failures = countif(Success == false), AvgMs = avg(DurationMs), MaxMs = max(DurationMs) by Tool
| order by Calls desc
| take 20
| extend TotalCalls = toscalar(calls | count)""".strip()

    @staticmethod
    def recent(service: str, window: str) -> str:
        s = kql_string(service)
        return f"""
AppDependencies
| where TimeGenerated > ago({window}) and AppRoleName == {s} and Name startswith "invoke_agent"
| top 20 by TimeGenerated desc
| project Time = TimeGenerated, OperationId, DurationMs, Success""".strip()

    @staticmethod
    def fleet(window: str) -> str:
        """One row per agent: runs, errors, latency (agent metrics) and tokens (the gateway), joined on the
        agent name. The platform, and so the workspace, is per environment."""
        return f"""
let runs = AppMetrics
| where TimeGenerated > ago({window}) and Name in ("agentkit.agent.runs", "agentkit.agent.run.duration")
| extend Agent = tostring(Properties["gen_ai.agent.name"]), Service = AppRoleName
| summarize Runs = sumif(Sum, Name == "agentkit.agent.runs"),
            Errors = sumif(Sum, Name == "agentkit.agent.runs" and tostring(Properties["outcome"]) == "error"),
            Blocked = sumif(Sum, Name == "agentkit.agent.runs" and tostring(Properties["outcome"]) == "blocked"),
            DurationSum = sumif(Sum, Name == "agentkit.agent.run.duration"),
            DurationCount = sumif(ItemCount, Name == "agentkit.agent.run.duration"),
            Services = make_set(Service, 5) by Agent;
let tokens = AppMetrics
| where TimeGenerated > ago({window}) and Name == "Total Tokens"
| summarize Tokens = sum(Sum) by Agent = tostring(Properties["Agent"]);
runs | join kind=fullouter tokens on Agent
| project Agent = coalesce(Agent, Agent1), Services, Runs = coalesce(todouble(Runs), 0.0),
          Errors = coalesce(todouble(Errors), 0.0), Blocked = coalesce(todouble(Blocked), 0.0),
          DurationSum = coalesce(todouble(DurationSum), 0.0), DurationCount = coalesce(todouble(DurationCount), 0.0),
          Tokens = coalesce(todouble(Tokens), 0.0)""".strip()


def _num(value: Any) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def shape_series(raw: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    series, totals = [], {"runs": 0.0, "errors": 0.0, "blocked": 0.0, "tokens": 0.0, "duration_sum": 0.0,
                          "duration_count": 0.0, "max_s": None}
    for r in raw:
        runs, count = _num(r.get("Runs")), _num(r.get("DurationCount"))
        avg = _num(r.get("DurationSum")) / count if count else None
        mx = r.get("DurationMax")
        series.append({"t": r.get("T"), "runs": runs, "errors": _num(r.get("Errors")), "blocked": _num(r.get("Blocked")),
                       "avg_s": avg, "max_s": _num(mx) if mx is not None else None, "tokens": _num(r.get("Tokens"))})
        totals["runs"] += runs
        totals["errors"] += _num(r.get("Errors"))
        totals["blocked"] += _num(r.get("Blocked"))
        totals["tokens"] += _num(r.get("Tokens"))
        totals["duration_sum"] += _num(r.get("DurationSum"))
        totals["duration_count"] += count
        if mx is not None:
            totals["max_s"] = max(totals["max_s"] or 0.0, _num(mx))
    dc = totals.pop("duration_count")
    ds = totals.pop("duration_sum")
    totals["avg_s"] = ds / dc if dc else None
    totals["error_rate"] = totals["errors"] / totals["runs"] if totals["runs"] else None
    return series, totals


class TtlCache:
    """A tiny per-key cache so a room full of people refreshing the console doesn't multiply queries."""

    def __init__(self, seconds: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self.seconds, self.clock = seconds, clock
        self._items: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any:
        hit = self._items.get(key)
        if hit and self.clock() - hit[0] < self.seconds:
            return hit[1]
        return None

    def put(self, key: str, value: Any) -> None:
        self._items[key] = (self.clock(), value)
