"""Orders for a small online shop."""

from dataclasses import dataclass, field


@dataclass
class LineItem:
    sku: str
    quantity: int
    unit_price: float


@dataclass
class Order:
    id: str
    items: list[LineItem] = field(default_factory=list)
    status: str = "pending"  # pending | paid | cancelled

    def total(self) -> float:
        return round(sum(i.quantity * i.unit_price for i in self.items), 2)

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
