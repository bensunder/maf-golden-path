"""Downstream APIs, as tools generated from their OpenAPI specs.

The carrier's live tracking API: auth, retries, tracing and response trimming come from
agentkit.tools. The team only chose which operations to expose and which fields the model sees.
"""

from __future__ import annotations

import os
from importlib.resources import files

from agentkit.tools import ApiClient, ManagedIdentityAuth, Shaper, openapi_tools

CARRIER = ApiClient(
    os.getenv("CARRIER_API_URL", "https://carrier.example.com/v1"),
    auth=ManagedIdentityAuth(os.getenv("CARRIER_API_SCOPE", "api://carrier-tracking/.default")),
    timeout=10,
)

CARRIER_TOOLS = openapi_tools(
    files(__package__) / "specs" / "carrier-api.yaml",
    client=CARRIER,
    operations=["getShipment"],  # read-only; redirectShipment is deliberately not exposed
    shapers={"getShipment": Shaper(fields=["trackingNumber", "status", "destination.city"])},
)
