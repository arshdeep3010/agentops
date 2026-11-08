import pytest

from shop.orders import LineItem, Order


def test_total_adds_up_line_items():
    order = Order(id="o1", items=[LineItem("a", 2, 10.0), LineItem("b", 1, 5.5)])
    assert order.total() == 25.5


def test_pay_marks_order_paid():
    order = Order(id="o1", items=[LineItem("a", 1, 10.0)])
    order.pay()
    assert order.status == "paid"


def test_cannot_pay_twice():
    order = Order(id="o1", items=[LineItem("a", 1, 10.0)])
    order.pay()
    with pytest.raises(ValueError):
        order.pay()


def test_cannot_pay_empty_order():
    with pytest.raises(ValueError):
        Order(id="o1").pay()


def test_cancel_pending_order():
    order = Order(id="o1", items=[LineItem("a", 1, 10.0)])
    order.cancel()
    assert order.status == "cancelled"
