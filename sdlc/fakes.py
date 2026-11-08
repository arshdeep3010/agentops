"""Offline stand-in for the model (LLM_MODE=fake): replays a recorded run of the refunds task on
the `shop` example, including one wrong first build, so tests and demos exercise the gate loop.
The `shoplite` example replays its own task instead (discount coupons, see fakes_shoplite.py).

Live mode (LLM_MODE=live) sends the real prompts to the model instead.
"""

import json

from engine.workflow import fake_llm
from sdlc import fakes_shoplite as shoplite

REQUIREMENTS = {
    "title": "Add refunds for paid orders",
    "summary": "Let staff refund paid orders, fully or in parts, never refunding more than was paid, "
               "and keep a record of every refund.",
    "user_stories": [
        {"id": "US-1", "as_a": "shop staff member", "i_want": "to refund a paid order fully or in parts",
         "so_that": "customers get their money back for returned or damaged items"},
        {"id": "US-2", "as_a": "shop owner", "i_want": "refunds never to exceed what the customer paid",
         "so_that": "we never pay out more than we took"},
    ],
    "acceptance_criteria": [
        {"id": "AC-1", "story_id": "US-1", "given": "a paid order with a total of 100.00",
         "when": "it is refunded 100.00", "then": "a refund of 100.00 is recorded, nothing is left to refund "
         "and the order status becomes 'refunded'"},
        {"id": "AC-2", "story_id": "US-1", "given": "a paid order with a total of 100.00",
         "when": "it is refunded 30.00 and then 20.00", "then": "both refunds are recorded in order and "
         "50.00 is left to refund; the status stays 'paid'"},
        {"id": "AC-3", "story_id": "US-2", "given": "a paid order with 20.00 left to refund",
         "when": "a refund of 30.00 is requested", "then": "a RefundError mentioning 'only 20.00 left' is "
         "raised and nothing is recorded"},
        {"id": "AC-4", "story_id": "US-2", "given": "an order that is not paid",
         "when": "a refund is requested", "then": "a RefundError mentioning 'not paid' is raised"},
        {"id": "AC-5", "story_id": "US-2", "given": "a paid order", "when": "a refund of 0 or less is requested",
         "then": "a RefundError mentioning 'positive' is raised"},
    ],
    "open_questions": ["Can a cancelled order be refunded? Assumed no: only paid orders can."],
    "out_of_scope": ["Moving real money", "Refunding individual line items"],
}

DESIGN = {
    "approach": "Add a refunds module with a RefundError, a frozen Refund record and a refund() function. "
                "Orders keep a list of their refunds and can report how much is left to refund.",
    "files": [
        {"path": "shop/refunds.py", "change": "create", "purpose": "Refund record, RefundError and refund()"},
        {"path": "shop/orders.py", "change": "modify", "purpose": "store refunds; add refundable()"},
    ],
    "test_files": ["tests/test_refunds.py"],
    "interfaces": [
        {"name": "refund", "signature": "def refund(order: Order, amount: float, reason: str = '') -> Refund",
         "description": "Validate and record a refund; marks the order 'refunded' when nothing is left"},
        {"name": "Order.refundable", "signature": "def refundable(self) -> float",
         "description": "Amount still refundable: total minus refunds for paid/refunded orders, else 0"},
        {"name": "RefundError", "signature": "class RefundError(ValueError)", "description": "Refund not allowed"},
    ],
    "risks": ["Float rounding on money; amounts are rounded to 2 decimals"],
    "adr": {"title": "Keep refunds on the order", "decision": "Order holds a list of Refund records",
            "alternatives": ["A separate refunds ledger"], "consequences": "Simple; no persistence layer needed"},
}

TESTS = '''import pytest

from shop.orders import LineItem, Order
from shop.refunds import RefundError, refund


def paid_order(total: float = 100.0) -> Order:
    order = Order(id="o1", items=[LineItem("sku-1", 1, total)])
    order.pay()
    return order


def test_full_refund_of_paid_order():
    order = paid_order(100.0)
    r = refund(order, 100.0, reason="damaged")
    assert r.amount == 100.0
    assert order.refundable() == 0
    assert order.status == "refunded"


def test_partial_refunds_add_up():
    order = paid_order(100.0)
    refund(order, 30.0)
    refund(order, 20.0)
    assert [r.amount for r in order.refunds] == [30.0, 20.0]
    assert order.refundable() == 50.0
    assert order.status == "paid"


def test_refund_more_than_left_is_rejected():
    order = paid_order(100.0)
    refund(order, 80.0)
    with pytest.raises(RefundError, match="only 20.00 left"):
        refund(order, 30.0)
    assert order.refundable() == 20.0


def test_refund_of_unpaid_order_is_rejected():
    order = Order(id="o2", items=[LineItem("sku-1", 1, 10.0)])
    with pytest.raises(RefundError, match="not paid"):
        refund(order, 5.0)


@pytest.mark.parametrize("amount", [0, -5])
def test_refund_amount_must_be_positive(amount):
    with pytest.raises(RefundError, match="positive"):
        refund(paid_order(), amount)
'''

ORDERS = '''"""Orders for a small online shop."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LineItem:
    sku: str
    quantity: int
    unit_price: float


@dataclass
class Order:
    id: str
    items: list[LineItem] = field(default_factory=list)
    status: str = "pending"  # pending | paid | refunded | cancelled
    refunds: list[Any] = field(default_factory=list)

    def total(self) -> float:
        return round(sum(i.quantity * i.unit_price for i in self.items), 2)

    def refundable(self) -> float:
        if self.status not in ("paid", "refunded"):
            return 0.0
        return round(self.total() - sum(r.amount for r in self.refunds), 2)

    def pay(self) -> None:
        if self.status != "pending":
            raise ValueError(f"cannot pay an order that is {self.status}")
        if not self.items:
            raise ValueError("cannot pay an empty order")
        self.status = "paid"

    def cancel(self) -> None:
        if self.status != "pending":
            raise ValueError(f"cannot cancel an order that is {self.status}")
        self.status = "cancelled"
'''

_REFUNDS = '''"""Refunds for paid orders."""

from dataclasses import dataclass
from datetime import datetime, timezone

from shop.orders import Order


class RefundError(ValueError):
    """A refund that isn't allowed."""


@dataclass(frozen=True)
class Refund:
    order_id: str
    amount: float
    reason: str
    at: datetime


def refund(order: Order, amount: float, reason: str = "") -> Refund:
    if amount <= 0:
        raise RefundError("refund amount must be positive")
    if order.status != "paid":
        raise RefundError(f"order {order.id} is not paid (status: {order.status})")
{check}    record = Refund(order.id, round(amount, 2), reason, datetime.now(timezone.utc))
    order.refunds.append(record)
    if order.refundable() == 0:
        order.status = "refunded"
    return record
'''
_LIMIT = '''    left = order.refundable()
    if amount > left + 1e-9:
        raise RefundError(f"only {left:.2f} left to refund")
'''
REFUNDS_WRONG = _REFUNDS.replace("{check}", "")      # first attempt forgets the limit: AC-3 fails
REFUNDS_RIGHT = _REFUNDS.replace("{check}", _LIMIT)


def _shoplite(ctx) -> bool:
    """The ShopLite example has its own replay (the coupons task); everything else replays refunds."""
    return ctx.input.get("example") == "shoplite"


@fake_llm("sdlc_requirements")
def _requirements(ctx, prompt, try_number):
    return json.dumps(shoplite.REQUIREMENTS if _shoplite(ctx) else REQUIREMENTS)


@fake_llm("sdlc_design")
def _design(ctx, prompt, try_number):
    return json.dumps(shoplite.DESIGN if _shoplite(ctx) else DESIGN)


@fake_llm("sdlc_tests")
def _tests(ctx, prompt, try_number):
    if _shoplite(ctx):
        return json.dumps({"files": [{"path": "tests/test_coupons.py", "content": shoplite.TESTS}],
                           "coverage_map": shoplite.COVERAGE_MAP})
    return json.dumps({"files": [{"path": "tests/test_refunds.py", "content": TESTS}], "coverage_map": [
        {"ac_id": "AC-1", "tests": ["test_full_refund_of_paid_order"]},
        {"ac_id": "AC-2", "tests": ["test_partial_refunds_add_up"]},
        {"ac_id": "AC-3", "tests": ["test_refund_more_than_left_is_rejected"]},
        {"ac_id": "AC-4", "tests": ["test_refund_of_unpaid_order_is_rejected"]},
        {"ac_id": "AC-5", "tests": ["test_refund_amount_must_be_positive"]},
    ]})


@fake_llm("sdlc_build")
def _build(ctx, prompt, try_number):
    if _shoplite(ctx):
        return json.dumps(shoplite.build(ctx.outputs["load_repo"]["files"], ctx.iteration))
    refunds = REFUNDS_WRONG if ctx.iteration == 0 else REFUNDS_RIGHT
    return json.dumps({"files": [{"path": "shop/refunds.py", "content": refunds},
                                 {"path": "shop/orders.py", "content": ORDERS}],
                       "notes": "first attempt" if ctx.iteration == 0 else "added the refund limit check"})


@fake_llm("sdlc_review")
def _review(ctx, prompt, try_number):
    if _shoplite(ctx):
        return json.dumps(shoplite.REVIEW)
    return json.dumps({"verdict": "approve", "comments": [
        {"file": "shop/refunds.py", "line": 16, "severity": "suggestion",
         "comment": "Money as float is fine for a demo; consider Decimal before real payments."},
        {"file": "shop/missing.py", "line": 3, "severity": "blocking",
         "comment": "This comment points at a file that isn't in the diff, so it must be dropped.",
         "scenario": "refund(order, 5) on a file that doesn't exist"},
    ]})
