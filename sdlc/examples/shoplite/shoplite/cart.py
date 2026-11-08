"""The shopping cart. Amounts are whole paise."""

from dataclasses import dataclass, field

from shoplite.catalog import Product, find


class CartError(ValueError):
    """Something the cart can't do: an unknown product or a bad quantity."""


@dataclass
class Line:
    product: Product
    quantity: int

    def amount(self) -> int:
        return self.product.price * self.quantity


@dataclass
class Cart:
    lines: dict[str, Line] = field(default_factory=dict)

    def add(self, product_id: str, quantity: int = 1) -> None:
        product = find(product_id)
        if product is None:
            raise CartError(f"unknown product {product_id!r}")
        if quantity < 1:
            raise CartError("quantity must be at least 1")
        line = self.lines.get(product_id)
        if line:
            line.quantity += quantity
        else:
            self.lines[product_id] = Line(product, quantity)

    def remove(self, product_id: str) -> None:
        self.lines.pop(product_id, None)

    def subtotal(self) -> int:
        return sum(line.amount() for line in self.lines.values())

    def total(self) -> int:
        return self.subtotal()

    def clear(self) -> None:
        self.lines.clear()
