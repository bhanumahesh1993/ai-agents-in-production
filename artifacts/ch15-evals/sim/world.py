"""Named world fixtures, and the faults they can be started under.

A case is a small world plus a claim about what should be true of that world
afterwards, so the world half needs a name you can put in a case record and
reproduce a year later.

One trap this module exists to avoid: a fixture can make success unavoidable.
If the world holds exactly one order and exactly one refundable item, an agent
that calls ``issue_refund`` with no reads at all passes the state grader, and
the case has measured nothing. Every fixture here keeps distractors — another
order on the same account, a second item that is not damaged, an order that
belongs to someone else — so a case is losable.
"""

from __future__ import annotations

from northstar_contracts import World

__all__ = ["FIXTURES", "from_fixture"]

ORDER = "NR-2026-0041827"          # two items, 8400c, delivered
MUG_ORDER = "NR-2026-0041903"      # one item, 3250c, flagged damaged
FRAUD_ORDER = "NR-2026-0042110"    # 24000c, another customer, fraud_review

#: Fixture name to the orders it keeps. Chapter 15's excerpt writes this as
#: ``World.from_fixture("two_item_delivered")``; it is a function here
#: because ``World`` belongs to ``northstar_contracts`` and an artifact does
#: not get to grow the contracts package a constructor one chapter needs.
FIXTURES: dict[str, tuple[str, ...]] = {
    # The case set's default. Two items on the claimed order, one of them
    # undamaged, plus a second order on the same account and one belonging
    # to a different customer.
    "two_item_delivered": (ORDER, MUG_ORDER, FRAUD_ORDER),
    # A single-item order, still with distractors around it.
    "single_item_damaged": (MUG_ORDER, ORDER, FRAUD_ORDER),
    # Only the flagged order is in scope.
    "fraud_flagged": (FRAUD_ORDER, ORDER),
    # Everything, for the cases about reading too widely.
    "account_with_distractors": (ORDER, MUG_ORDER, FRAUD_ORDER),
}


def from_fixture(name: str) -> World:
    """Build a fresh world from a named fixture.

    Raises:
        KeyError: On an unknown fixture name, listing the ones that exist.
            A case that names a fixture nobody wrote is ungradeable, and an
            ungradeable case is a failure rather than a skip.
    """
    if name not in FIXTURES:
        known = ", ".join(sorted(FIXTURES))
        raise KeyError(f"no fixture {name!r}; known fixtures: {known}")
    world = World()
    keep = set(FIXTURES[name])
    for order_id in list(world.orders):
        if order_id not in keep:
            del world.orders[order_id]
    return world
