"""Order-status tools.

The in-memory ``_ORDERS`` table stands in for the order API. In a real service these
functions call the commerce API with the managed identity (e.g. via httpx + azure.identity).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from agent_framework import tool
from pydantic import Field

_ORDERS: dict[str, dict[str, Any]] = {
    "A1001": {"status": "shipped", "carrier": "UPS", "tracking": "1Z999AA10123456784", "total": 42.50, "eta": "2026-09-26"},
    "A1002": {"status": "processing", "carrier": None, "tracking": None, "total": 129.00, "eta": "2026-09-30"},
    "A1003": {"status": "delivered", "carrier": "USPS", "tracking": "9400100000000000000000", "total": 18.99, "eta": None},
}

REFUND_LIMIT = 50.00
TICKETS: list[dict[str, str]] = []  # observable in tests; a real impl posts to the ticketing API
REFUNDS: list[dict[str, Any]] = []

OrderId = Annotated[str, Field(description="Order id, e.g. 'A1001'", pattern=r"^[A-Za-z]\d{4}$")]


@tool
def lookup_order(order_id: OrderId) -> dict[str, Any] | str:
    """Get status, carrier, tracking number, total and ETA for an order."""
    order = _ORDERS.get(order_id.upper())
    if order is None:
        return f"No order found with id {order_id}."
    return {"order_id": order_id.upper(), **order}


@tool
def issue_refund(
    order_id: OrderId,
    amount: Annotated[float, Field(description="Refund amount in USD", gt=0)],
    reason: Annotated[str, Field(description="Short reason given by the customer")],
) -> str:
    """Refund part or all of an order, up to the self-service limit."""
    order = _ORDERS.get(order_id.upper())
    if order is None:
        return f"No order found with id {order_id}."
    REFUNDS.append({"order_id": order_id.upper(), "amount": amount, "reason": reason})
    return f"Refunded ${amount:.2f} on order {order_id.upper()}."


@tool
def escalate_to_human(
    order_id: OrderId,
    summary: Annotated[str, Field(description="One-paragraph summary for the support specialist")],
) -> str:
    """Open a ticket for a human support specialist."""
    ticket_id = f"SUP-{uuid.uuid4().hex[:6].upper()}"
    TICKETS.append({"ticket_id": ticket_id, "order_id": order_id.upper(), "summary": summary})
    return f"Created ticket {ticket_id}; a specialist will follow up within one business day."


def _refund_policy(args: dict[str, Any]) -> str | None:
    order = _ORDERS.get(str(args.get("order_id", "")).upper())
    amount = float(args.get("amount", 0))
    if amount > REFUND_LIMIT:
        return f"refunds over ${REFUND_LIMIT:.0f} need a specialist; use escalate_to_human instead"
    if order and amount > order["total"]:
        return f"amount exceeds the order total (${order['total']:.2f})"
    return None


TOOLS = [lookup_order, issue_refund, escalate_to_human]

# Central policy: enforced by agentkit before the tool runs, whatever the model decides.
TOOL_POLICY: dict = {"denied": [], "validators": {"issue_refund": _refund_policy}}
