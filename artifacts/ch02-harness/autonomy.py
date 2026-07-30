"""The autonomy budget as a file the runtime reads, not a document.

Chapter 1 says autonomy is a budget with eight line items rather than a dial.
A worksheet that produces a document is a governance exercise. A worksheet
that produces a file the guard and the policy engine load at startup is a
control, and the difference is testable: :func:`unenforced` names every axis
in ``autonomy_budget.yaml`` that no component actually reads, and the test
suite fails on a non-empty list. Unset axes fail too, rather than defaulting
to unlimited, because unset *is* unlimited and that is how Northstar's agent
came to have seven of eight axes open on the day of the incident.

The eight axes and where each is enforced:

===================  ==========================================
action_scope         the registered tool surface
resource_scope       :class:`AutonomyPolicy`, per call
duration             :class:`~budget.BudgetGuard`, active seconds
step_budget          the guard's turn cap, the policy's call cap
financial_budget     the guard's cents cap, the policy's thresholds
data_budget          the tool contract's page cap, and mock mode
human_control        :class:`AutonomyPolicy`, per call
blast_radius         :class:`AutonomyPolicy`, per run
===================  ==========================================

There is no YAML dependency. ``pip install -e .`` pulls nothing at all, on
purpose, so this module carries a parser for the small subset of YAML the
file uses: block mappings, flow lists, flow mappings, comments, integers,
and strings. Anything fancier and the loader should raise rather than guess.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from budget import BudgetGuard
from journal import StepJournal
from northstar_contracts import Money, ToolCall
from northstar_policy import Decision, Principal
from registry import HarnessRegistry

__all__ = [
    "AXES",
    "AutonomyBudget",
    "AutonomyPolicy",
    "Wiring",
    "guard_for",
    "load_budget",
    "unenforced",
]

BUDGET_PATH = Path(__file__).resolve().parent / "autonomy_budget.yaml"

#: The eight axes. A file missing any of them is a file that left an axis
#: unbounded, which is a different defect from setting it generously.
AXES: tuple[str, ...] = (
    "action_scope",
    "resource_scope",
    "duration",
    "step_budget",
    "financial_budget",
    "data_budget",
    "human_control",
    "blast_radius",
)

#: Which action class each Northstar tool belongs to. The classes come from
#: Chapter 1's action-scope axis; the mapping is data so that adding a tool
#: means editing this table and the budget file, not inheriting a default.
ACTION_CLASS: dict[str, str] = {
    "get_order": "read",
    "get_policy": "read",
    "search_orders": "read",
    "send_message": "mutate",
    "escalate_to_specialist": "mutate",
    "issue_refund": "mutate",
}


# --------------------------------------------------------------- the parser


def _scalar(text: str) -> Any:
    """Parse one YAML scalar: integer, boolean, null, or string."""
    token = text.strip()
    if token in ("~", "null", ""):
        return None
    if token in ("true", "false"):
        return token == "true"
    if token.lstrip("-").isdigit():
        return int(token)
    return token.strip("'\"")


def _flow(text: str) -> Any:
    """Parse a flow list ``[a, b]`` or a flow mapping ``{k: v}``."""
    body = text[1:-1].strip()
    if not body:
        return [] if text.startswith("[") else {}
    parts = [p.strip() for p in body.split(",")]
    if text.startswith("["):
        return [_scalar(p) for p in parts]
    pairs = [p.split(":", 1) for p in parts]
    return {k.strip(): _scalar(v) for k, v in pairs}


def _value(text: str) -> Any:
    """Parse any right-hand side."""
    token = text.strip()
    if token.startswith(("[", "{")):
        return _flow(token)
    return _scalar(token)


def parse_yaml(text: str) -> dict[str, Any]:
    """Parse the YAML subset ``autonomy_budget.yaml`` uses.

    Block mappings nested by indentation, flow lists, flow mappings, and
    ``#`` comments. Raises on anything else rather than guessing, because a
    config loader that silently mis-parses a budget is worse than one that
    refuses to start.

    Example:
        >>> parse_yaml("a: 1\\nb: {c: 2}\\nd:\\n  e: [x, y]\\n")
        {'a': 1, 'b': {'c': 2}, 'd': {'e': ['x', 'y']}}
    """
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if ":" not in line:
            raise ValueError(f"unsupported YAML line: {raw!r}")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise ValueError(f"bad indentation at {raw!r}")
        parent = stack[-1][1]
        key, _, rest = line.strip().partition(":")
        if rest.strip():
            parent[key.strip()] = _value(rest)
        else:
            child: dict[str, Any] = {}
            parent[key.strip()] = child
            stack.append((indent, child))
    return root


# ------------------------------------------------------------- the document


@dataclass(frozen=True)
class AutonomyBudget:
    """One agent's eight axes, as the runtime sees them."""

    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def agent(self) -> str:
        """The agent this budget belongs to."""
        return str(self.raw.get("agent", "unnamed"))

    @property
    def risk_tier(self) -> int:
        """The tier the eight axes add up to."""
        return int(self.raw.get("risk_tier", 0))

    def axis(self, name: str) -> Any:
        """One axis, or ``None`` when the file left it unset."""
        return self.raw.get(name)

    def missing_axes(self) -> list[str]:
        """Axes the file does not set. Unset means unbounded."""
        return [a for a in AXES if not self.raw.get(a)]

    # -- the numbers the runtime needs -------------------------------------
    #
    # Every accessor below fails closed. An axis the file does not set reads
    # as zero, or as an empty list of permitted verbs, rather than raising or
    # defaulting to generous. Unset is the state seven of Northstar's eight
    # axes were in on the day of the incident, and it behaved as unlimited
    # because nothing was there to say otherwise.

    def _under(self, axis: str, key: str, default: Any) -> Any:
        """One key of a block-mapping axis, or ``default`` if it is unset."""
        block = self.raw.get(axis)
        if not isinstance(block, dict):
            return default
        value = block.get(key)
        return default if value is None else value

    @property
    def max_turns(self) -> int:
        """Model calls per run."""
        return int(self._under("step_budget", "max_turns", 0))

    @property
    def max_tool_calls(self) -> int:
        """Tool calls per run, across all turns."""
        return int(self._under("step_budget", "max_tool_calls", 0))

    @property
    def max_run_seconds(self) -> float:
        """Active seconds per run, suspensions excluded."""
        return float(self._under("duration", "max_run_seconds", 0.0))

    @property
    def model_cents_per_run(self) -> Money:
        """Model spend per run, in integer cents."""
        return int(self._under("financial_budget", "model_cents_per_run", 0))

    @property
    def transaction_cents_per_action(self) -> Money:
        """Above this, a human decides. Northstar's number is 5000."""
        return int(
            self._under("financial_budget", "transaction_cents_per_action", 0)
        )

    @property
    def transaction_cents_per_run(self) -> Money:
        """Cumulative money one run may move."""
        return int(
            self._under("financial_budget", "transaction_cents_per_run", 0)
        )

    @property
    def max_records(self) -> int:
        """Records one run may read."""
        return int(self._under("data_budget", "max_records", 0))

    @property
    def egress(self) -> str:
        """Where results may leave to. ``none`` is what mock mode promises."""
        return str(self._under("data_budget", "egress", "unknown"))

    @property
    def action_scope(self) -> list[str]:
        """The verbs this agent may use."""
        return list(self.raw.get("action_scope") or [])

    @property
    def max_customers_per_run(self) -> int:
        """Blast radius, in customers."""
        return int(self._under("blast_radius", "max_customers_per_run", 0))

    def human_control(self, level: str) -> list[str]:
        """Tools at one control level: automatic, sampled, approval_required."""
        return list(self._under("human_control", level, []))

    @property
    def declared_tools(self) -> set[str]:
        """Every tool the file assigns a control level to.

        The closed tool surface. A tool that is not here cannot be
        registered, which is how ``max_depth: 1`` is enforced: a delegate or
        spawn tool would have to be written into this file first.
        """
        return {
            name
            for level in ("automatic", "sampled", "approval_required")
            for name in self.human_control(level)
        }


def load_budget(path: str | Path = BUDGET_PATH) -> AutonomyBudget:
    """Read ``autonomy_budget.yaml``."""
    return AutonomyBudget(parse_yaml(Path(path).read_text(encoding="utf-8")))


# ------------------------------------------------------------ enforcement


class AutonomyPolicy:
    """A policy decision point that reads the budget file.

    Five axes are per-call decisions, so they belong here rather than in a
    guard: resource scope, human control, the two transaction limits, and
    blast radius. The tool-call cap is here too, because the dispatch path is
    the only place that sees every call.

    State is per run. :meth:`start` resets it, and the loop's own run
    identity is what a resumed run presents, so a resume does not receive a
    second full allowance.

    Args:
        budget: The loaded worksheet.
        customer_id: The requesting customer, from admission. Not from the
            model, and not from a tool argument.
        orders: Order id to owning customer id, so a resource-scope decision
            does not have to query the system it is protecting.
    """

    def __init__(
        self,
        budget: AutonomyBudget,
        customer_id: str,
        orders: dict[str, str],
    ) -> None:
        self.budget = budget
        self.customer_id = customer_id
        self.orders = dict(orders)
        self.calls = 0
        self.cents_moved: Money = 0
        self.customers: set[str] = set()
        self.decisions: list[tuple[str, Decision, str]] = []

    def start(self) -> AutonomyPolicy:
        """Reset the per-run counters. Returns ``self``."""
        self.calls = 0
        self.cents_moved = 0
        self.customers = set()
        self.decisions = []
        return self

    def evaluate(
        self,
        principal: Principal,
        call: ToolCall,
        ctx: dict[str, Any],
    ) -> Decision:
        """Decide one call against the budget. First failing axis wins."""
        decision, axis = self._decide(call)
        self.decisions.append((call.name, decision, axis))
        if decision is Decision.ALLOW:
            self.calls += 1
            if call.name == "issue_refund":
                self.cents_moved += int(call.arguments.get("amount_cents", 0))
            owner = self.orders.get(str(call.arguments.get("order_id", "")))
            if owner:
                self.customers.add(owner)
        return decision

    def _decide(self, call: ToolCall) -> tuple[Decision, str]:
        """The rule list, in order, with the axis each rule comes from."""
        if call.name not in self.budget.declared_tools:
            return Decision.DENY, "action_scope"
        if ACTION_CLASS.get(call.name, "delete") not in self.budget.action_scope:
            return Decision.DENY, "action_scope"
        if self.calls >= self.budget.max_tool_calls:
            return Decision.DENY, "step_budget"

        order_id = str(call.arguments.get("order_id", ""))
        if order_id and self.orders.get(order_id) != self.customer_id:
            return Decision.DENY, "resource_scope"
        if order_id:
            reach = self.customers | {self.orders.get(order_id, "")}
            if len(reach - {""}) > self.budget.max_customers_per_run:
                return Decision.DENY, "blast_radius"

        if call.name == "issue_refund":
            amount = int(call.arguments.get("amount_cents", 0))
            if amount >= self.budget.transaction_cents_per_action:
                return Decision.REQUIRE_APPROVAL, "financial_budget"
            if (
                self.cents_moved + amount
                > self.budget.transaction_cents_per_run
            ):
                return Decision.DENY, "financial_budget"

        if call.name in self.budget.human_control("approval_required"):
            if "amount_cents" in call.arguments:
                # A money-bearing call was already weighed against the
                # transaction threshold above. Under the threshold, the
                # class default is satisfied by the threshold itself:
                # that is what putting a number on the axis bought.
                return Decision.ALLOW, "human_control"
            return Decision.REQUIRE_APPROVAL, "human_control"
        return Decision.ALLOW, "human_control"


def guard_for(
    budget: AutonomyBudget,
    journal: StepJournal | None = None,
) -> BudgetGuard:
    """Build the guard the budget file describes.

    Three axes come straight out of the file: the turn cap, the money cap,
    and the active-seconds deadline. Nothing here is a default someone
    inherited from a framework.
    """
    return BudgetGuard(
        max_turns=budget.max_turns,
        budget_cents=budget.model_cents_per_run,
        deadline_s=budget.max_run_seconds,
        journal=journal,
    )


@dataclass(frozen=True)
class Wiring:
    """The components an axis can be enforced by, for the audit."""

    guard: BudgetGuard
    tools: HarnessRegistry
    policy: AutonomyPolicy


def unenforced(budget: AutonomyBudget, wiring: Wiring) -> list[str]:
    """Axes with no component that actually reads them.

    Each check compares the file against the live object, not against
    another copy of the file. A number written twice is not a control; a
    number the guard is holding is.

    Returns:
        The axes that failed, plus any axis the file left unset. An empty
        list is the only passing result.
    """
    missing = set(budget.missing_axes())
    problems = [f"{axis}: unset" for axis in AXES if axis in missing]
    checks = {
        "action_scope": _action_scope_ok,
        "resource_scope": _resource_scope_ok,
        "duration": lambda b, w: w.guard.deadline_s == b.max_run_seconds,
        "step_budget": _step_budget_ok,
        "financial_budget": _financial_ok,
        "data_budget": _data_budget_ok,
        "human_control": _human_control_ok,
        "blast_radius": _blast_radius_ok,
    }
    for axis, check in checks.items():
        if axis in missing:
            continue    # already reported, and unset reads as fail-closed
        if not check(budget, wiring):
            problems.append(f"{axis}: no enforcement point")
    return problems


def _call(name: str, **arguments: Any) -> ToolCall:
    """A probe call, for asking the policy engine a hypothetical."""
    return ToolCall(id=f"probe-{name}", name=name, arguments=arguments)


def _action_scope_ok(budget: AutonomyBudget, wiring: Wiring) -> bool:
    """Every registered tool is declared, and its verb is in scope."""
    registered = set(wiring.tools.names())
    if not registered <= budget.declared_tools:
        return False
    return all(
        ACTION_CLASS.get(name, "delete") in budget.action_scope
        for name in registered
    )


def _resource_scope_ok(budget: AutonomyBudget, wiring: Wiring) -> bool:
    """A read of another customer's order is denied."""
    foreign = next(
        (
            order_id
            for order_id, owner in wiring.policy.orders.items()
            if owner != wiring.policy.customer_id
        ),
        None,
    )
    if foreign is None:
        return False
    decision = wiring.policy._decide(_call("get_order", order_id=foreign))
    return decision == (Decision.DENY, "resource_scope")


def _step_budget_ok(budget: AutonomyBudget, wiring: Wiring) -> bool:
    """The guard holds the turn cap and the policy holds the call cap."""
    if wiring.guard.max_turns != budget.max_turns:
        return False
    saturated = AutonomyPolicy(
        budget, wiring.policy.customer_id, wiring.policy.orders
    )
    saturated.calls = budget.max_tool_calls
    order_id = next(iter(wiring.policy.orders))
    decision, axis = saturated._decide(_call("get_order", order_id=order_id))
    return (decision, axis) == (Decision.DENY, "step_budget")


def _financial_ok(budget: AutonomyBudget, wiring: Wiring) -> bool:
    """The guard holds the model spend; the policy holds both money limits."""
    if wiring.guard.budget_cents != budget.model_cents_per_run:
        return False
    order_id = _own_order(wiring)
    at_threshold = _call(
        "issue_refund",
        order_id=order_id,
        amount_cents=budget.transaction_cents_per_action,
        reason="damaged",
    )
    below = _call(
        "issue_refund",
        order_id=order_id,
        amount_cents=budget.transaction_cents_per_action - 1,
        reason="damaged",
    )
    fresh = AutonomyPolicy(
        budget, wiring.policy.customer_id, wiring.policy.orders
    )
    gated = fresh._decide(at_threshold)[0] is Decision.REQUIRE_APPROVAL
    allowed = fresh._decide(below)[0] is Decision.ALLOW

    spent = AutonomyPolicy(
        budget, wiring.policy.customer_id, wiring.policy.orders
    )
    spent.cents_moved = budget.transaction_cents_per_run
    capped = spent._decide(below) == (Decision.DENY, "financial_budget")
    return gated and allowed and capped


def _data_budget_ok(budget: AutonomyBudget, wiring: Wiring) -> bool:
    """The search contract cannot return more than the record budget.

    And ``egress: none`` is the mock-mode promise: importing the runtime
    imports no provider SDK, so nothing can leave the process even if a
    tool wanted it to.
    """
    spec = wiring.tools.spec_for("search_orders")
    if spec is None:
        return False
    page_size = spec.input_schema["properties"]["page_size"]
    if int(page_size["maximum"]) > budget.max_records:
        return False
    if budget.egress != "none":
        return True
    return not {"anthropic", "openai"} & set(sys.modules)


def _human_control_ok(budget: AutonomyBudget, wiring: Wiring) -> bool:
    """Every tool has exactly one level, and the gated ones are gated."""
    levels = ["automatic", "sampled", "approval_required"]
    assignments = [
        sum(1 for level in levels if name in budget.human_control(level))
        for name in wiring.tools.names()
    ]
    if not assignments or any(count != 1 for count in assignments):
        return False
    gated = [
        name
        for name in budget.human_control("approval_required")
        if name != "issue_refund"
    ]
    if not gated:
        return False
    fresh = AutonomyPolicy(
        budget, wiring.policy.customer_id, wiring.policy.orders
    )
    order_id = _own_order(wiring)
    return all(
        fresh._decide(_call(name, order_id=order_id, reason="fraud_suspected"))
        == (Decision.REQUIRE_APPROVAL, "human_control")
        for name in gated
    )


def _blast_radius_ok(budget: AutonomyBudget, wiring: Wiring) -> bool:
    """A second customer's order in the same run is denied."""
    others = {
        order_id: owner
        for order_id, owner in wiring.policy.orders.items()
        if owner != wiring.policy.customer_id
    }
    if not others:
        return False
    probe = AutonomyPolicy(
        budget,
        wiring.policy.customer_id,
        {**wiring.policy.orders, **dict.fromkeys(others, wiring.policy.customer_id)},
    )
    probe.customers = {"CUST-OTHER"}
    order_id = next(iter(others))
    return probe._decide(_call("get_order", order_id=order_id)) == (
        Decision.DENY,
        "blast_radius",
    )


def _own_order(wiring: Wiring) -> str:
    """An order the requesting customer owns."""
    return next(
        order_id
        for order_id, owner in wiring.policy.orders.items()
        if owner == wiring.policy.customer_id
    )
