"""What the shop sells. Prices are whole paise (₹1 = 100 paise), so money is never a float."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    price: int  # paise
    emoji: str
    stock: int


PRODUCTS: tuple[Product, ...] = (
    Product("tote", "Canvas Tote Bag", 79900, "👜", 25),
    Product("mug", "Ceramic Mug", 44900, "☕", 40),
    Product("notebook", "Dotted Notebook", 29900, "📓", 60),
    Product("lamp", "Desk Lamp", 189900, "💡", 8),
    Product("mouse", "Wireless Mouse", 129900, "🖱️", 15),
    Product("bottle", "Steel Water Bottle", 64900, "🧴", 30),
)


def find(product_id: str) -> Product | None:
    return next((p for p in PRODUCTS if p.id == product_id), None)
