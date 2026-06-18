"""Agent tool registry.

Wraps the two existing subsystems — the GraphQL client (Module 1) and the business-rules
engine (Module 2) — as a small set of named, JSON-safe callables. The LangGraph graph
invokes these by name; it does NOT let the LLM free-call them. Keeping the registry
explicit means every tool is auditable and the rule decisions (refund, commission, …)
come from the versioned engine, never the model.

Each call returns a ToolResult so the graph can branch on ok/error without exceptions
leaking into the state machine.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Optional

from scripts.config.loader import Config, load_config
from scripts.graphql_client.client import MarketplaceClient
from scripts.rules.commission import compute_commission
from scripts.rules.eligibility import PROCESSES, qualifies_for
from scripts.rules.models import OrderView
from scripts.rules.returns import is_returnable


def _jsonable(obj: Any) -> Any:
    """Coerce rule-engine dataclasses (which carry Decimal money) into JSON-safe values."""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: Optional[str] = None


# Declarative catalog — used for telemetry, the improvement loop, and (later) to describe
# the tools to the LLM. The graph maps an intent to one of these names.
TOOL_SPECS: list[dict] = [
    {"name": "fetch_order", "desc": "Fetch a single order by id.", "params": ["order_id"]},
    {"name": "list_buyer_orders", "desc": "List a buyer's recent orders.", "params": ["buyer_id", "first"]},
    {"name": "check_return", "desc": "Decide if an order is returnable (14-day window, category, Smart!).", "params": ["order_id"]},
    {"name": "check_eligibility", "desc": "Decide if an order qualifies for a process (refund/dispute/buyer_protection).", "params": ["order_id", "process"]},
    {"name": "compute_order_commission", "desc": "Compute seller commission for an order.", "params": ["order_id"]},
    {"name": "submit_return", "desc": "Open a return request (mutation).", "params": ["order_id", "reason"]},
]


class AgentTools:
    def __init__(self, client: Optional[MarketplaceClient] = None, config: Optional[Config] = None):
        self.client = client or MarketplaceClient()
        self.config = config or load_config()

    # --- retrieval (GraphQL client) -------------------------------------------------
    def fetch_order(self, order_id: str) -> ToolResult:
        order = self.client.get_order(order_id)
        if not order:
            return ToolResult(False, error=f"order {order_id} not found")
        return ToolResult(True, _jsonable(order))

    def list_buyer_orders(self, buyer_id: str, first: int = 5) -> ToolResult:
        conn = self.client.list_buyer_orders(buyer_id, first=first)
        if not conn or conn.get("totalCount", 0) == 0:
            return ToolResult(False, error=f"no orders found for buyer {buyer_id}")
        return ToolResult(True, _jsonable(conn))

    # --- decisions (rules engine over a normalized OrderView) -----------------------
    def _order_view(self, order_id: str) -> Optional[OrderView]:
        order = self.client.get_order(order_id)
        return OrderView.from_graphql_order(order) if order else None

    def check_return(self, order_id: str) -> ToolResult:
        view = self._order_view(order_id)
        if view is None:
            return ToolResult(False, error=f"order {order_id} not found")
        decision = is_returnable(view, self.config.rules)
        return ToolResult(True, _jsonable(asdict(decision)))

    def check_eligibility(self, order_id: str, process: str) -> ToolResult:
        if process not in PROCESSES:
            return ToolResult(False, error=f"unknown process {process!r}; expected {PROCESSES}")
        view = self._order_view(order_id)
        if view is None:
            return ToolResult(False, error=f"order {order_id} not found")
        decision = qualifies_for(process, view, self.config.rules)
        return ToolResult(True, _jsonable(asdict(decision)))

    def compute_order_commission(self, order_id: str) -> ToolResult:
        view = self._order_view(order_id)
        if view is None:
            return ToolResult(False, error=f"order {order_id} not found")
        breakdown = compute_commission(view, self.config.rules)
        return ToolResult(True, _jsonable(asdict(breakdown)))

    # --- mutation -------------------------------------------------------------------
    # DELIBERATE BOUNDARY: submit_return is a state-changing write, so it is intentionally NOT
    # wired to any LLM-classified intent in graph.INTENT_TOOL. The agent reads freely but never
    # opens a return on its own — that action is reserved for an explicit, confirmed user request
    # (same philosophy as letting the rules engine, not the model, decide anything financial).
    # It stays implemented + callable so a confirmation UI / explicit endpoint can invoke it.
    def submit_return(self, order_id: str, reason: str) -> ToolResult:
        result = self.client.request_return(order_id, reason)
        if not result:
            return ToolResult(False, error=f"could not open return for {order_id}")
        return ToolResult(True, _jsonable(result))

    def call(self, name: str, **kwargs) -> ToolResult:
        """Dispatch by tool name (used by the graph + telemetry)."""
        fn = getattr(self, name, None)
        if fn is None or name not in {s["name"] for s in TOOL_SPECS}:
            return ToolResult(False, error=f"unknown tool {name!r}")
        try:
            return fn(**kwargs)
        except Exception as exc:  # keep tool failures out of the state machine
            return ToolResult(False, error=f"{name} failed: {exc}")
