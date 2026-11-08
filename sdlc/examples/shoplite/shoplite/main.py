"""ShopLite: a tiny online shop. A FastAPI backend and one web page (static/index.html).

Run it: uvicorn shoplite.main:app   then open http://127.0.0.1:8000
State lives in memory (one shared cart), which is enough for a demo.
"""

from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from shoplite.cart import Cart, CartError
from shoplite.catalog import PRODUCTS

PAGE = Path(__file__).parent / "static" / "index.html"


class AddItem(BaseModel):
    product_id: str
    quantity: int = 1


def cart_json(cart: Cart) -> dict:
    return {
        "lines": [
            {"product_id": line.product.id, "name": line.product.name, "emoji": line.product.emoji,
             "price": line.product.price, "quantity": line.quantity, "amount": line.amount()}
            for line in cart.lines.values()
        ],
        "subtotal": cart.subtotal(),
        "total": cart.total(),
    }


def create_app() -> FastAPI:
    app = FastAPI(title="ShopLite")
    cart = Cart()
    orders: list[dict] = []

    @app.get("/", response_class=HTMLResponse)
    def page() -> str:
        return PAGE.read_text(encoding="utf-8")

    @app.get("/api/products")
    def products() -> list[dict]:
        return [asdict(p) for p in PRODUCTS]

    @app.get("/api/cart")
    def get_cart() -> dict:
        return cart_json(cart)

    @app.post("/api/cart/items")
    def add_item(body: AddItem) -> dict:
        try:
            cart.add(body.product_id, body.quantity)
        except CartError as exc:
            raise HTTPException(400, str(exc)) from exc
        return cart_json(cart)

    @app.delete("/api/cart/items/{product_id}")
    def remove_item(product_id: str) -> dict:
        cart.remove(product_id)
        return cart_json(cart)

    @app.post("/api/checkout")
    def checkout() -> dict:
        if not cart.lines:
            raise HTTPException(400, "your cart is empty")
        order = {"id": len(orders) + 1, **cart_json(cart)}
        orders.append(order)
        cart.clear()
        return order

    return app


app = create_app()
