"""The Azure Monitor client and query shaping behind the live charts."""

import httpx
import pytest

from agentkit.channels.traffic import AzureLogsClient, LogsQueryError, TrafficQueries, kql_string, rows, shape_series


def test_kql_strings_are_escaped():
    assert kql_string('a"b\\c') == '"a\\"b\\\\c"'
    assert kql_string("x\ny") == '"x y"'


def test_rows_from_the_query_api_shape():
    body = {"tables": [{"columns": [{"name": "A"}, {"name": "B"}], "rows": [[1, "x"], [2, "y"]]}]}
    assert rows(body) == [{"A": 1, "B": "x"}, {"A": 2, "B": "y"}]
    assert rows({"tables": []}) == []


def test_shape_series_handles_missing_latency():
    series, totals = shape_series([{"T": "t", "Runs": 0, "Tokens": 50, "DurationCount": 0, "DurationMax": None}])
    assert series[0]["avg_s"] is None and series[0]["max_s"] is None
    assert totals["avg_s"] is None and totals["error_rate"] is None and totals["tokens"] == 50


async def test_client_posts_a_resource_centric_query_with_a_bearer_token():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"], seen["body"] = str(request.url), request.headers["authorization"], request.content
        return httpx.Response(200, json={"tables": [{"columns": [{"name": "Runs"}], "rows": [[3]]}]})

    async def token():
        return "tok"

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AzureLogsClient("/subscriptions/s/resourceGroups/g/providers/microsoft.insights/components/appi", token,
                                 client=http)
        result = await client.query("AppMetrics | take 1", "PT1H")
    assert result == [{"Runs": 3}]
    assert seen["url"] == "https://api.loganalytics.io/v1/subscriptions/s/resourceGroups/g/providers/microsoft.insights/components/appi/query"
    assert seen["auth"] == "Bearer tok" and b'"timespan":"PT1H"' in seen["body"].replace(b" ", b"")


@pytest.mark.parametrize("status,text", [(403, "Monitoring Reader"), (500, "HTTP 500")])
async def test_client_errors_are_readable(status, text):
    async def token():
        return "tok"

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, text="nope"))) as http:
        with pytest.raises(LogsQueryError, match=text):
            await AzureLogsClient("/workspaces/w", token, client=http).query("x", "PT1H")


async def test_client_without_a_token_says_so():
    async def token():
        raise RuntimeError("no identity")

    with pytest.raises(LogsQueryError, match="token"):
        await AzureLogsClient("/workspaces/w", token).query("x", "PT1H")


def test_fleet_query_groups_by_agent():
    kql = TrafficQueries.fleet("24h")
    assert "by Agent" in kql and "Total Tokens" in kql and "ago(24h)" in kql


async def test_partial_results_are_refused_not_shown():
    async def token():
        return "tok"

    body = {"tables": [{"columns": [{"name": "Runs"}], "rows": [[1]]}], "error": {"code": "PartialError"}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))) as http:
        with pytest.raises(LogsQueryError, match="incomplete"):
            await AzureLogsClient("/workspaces/w", token, client=http).query("x", "PT1H")


def test_counts_are_typed_consistently_for_coalesce():
    for kql in (TrafficQueries.series("a", "s", "prod", "1h", "24h"), TrafficQueries.fleet("24h")):
        assert "coalesce(DurationCount, 0.0)" not in kql and "coalesce(todouble(DurationCount), 0.0)" in kql
