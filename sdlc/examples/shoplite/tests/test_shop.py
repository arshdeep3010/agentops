from fastapi.testclient import TestClient

from shoplite.main import create_app


def client() -> TestClient:
    return TestClient(create_app())


def test_page_is_served():
    r = client().get("/")
    assert r.status_code == 200
    assert "ShopLite" in r.text


def test_products_are_listed_with_prices_in_paise():
    products = client().get("/api/products").json()
    lamp = next(p for p in products if p["id"] == "lamp")
    assert lamp["price"] == 189900


def test_adding_items_updates_the_total():
    c = client()
    c.post("/api/cart/items", json={"product_id": "mug", "quantity": 2})
    cart = c.post("/api/cart/items", json={"product_id": "notebook"}).json()
    assert cart["subtotal"] == 2 * 44900 + 29900
    assert cart["total"] == cart["subtotal"]


def test_unknown_product_is_rejected():
    r = client().post("/api/cart/items", json={"product_id": "rocket"})
    assert r.status_code == 400


def test_removing_an_item():
    c = client()
    c.post("/api/cart/items", json={"product_id": "mug"})
    assert c.delete("/api/cart/items/mug").json()["lines"] == []


def test_checkout_creates_an_order_and_empties_the_cart():
    c = client()
    c.post("/api/cart/items", json={"product_id": "tote"})
    order = c.post("/api/checkout").json()
    assert order["id"] == 1 and order["total"] == 79900
    assert c.get("/api/cart").json()["lines"] == []


def test_checkout_of_an_empty_cart_is_rejected():
    assert client().post("/api/checkout").status_code == 400
