"""Offline model replies for the ShopLite demo (LLM_MODE=fake): the "add discount coupons" task.

Like the `shop` replay, the first build has a bug on purpose (the coupon code is matched case-
sensitively, so "save10" is refused) and the locked tests catch it, so the demo shows the gate loop.
Changed files are made by patching the real example files, so the replay can't drift from them.
"""

TASK = ("Add discount coupons. The code SAVE10 takes 10% off the cart at checkout, in any letter case. "
        "Show a coupon box in the cart, and reject codes the shop doesn't know.")

REQUIREMENTS = {
    "title": "Add discount coupons to the cart",
    "summary": "Shoppers can enter a coupon code in the cart. SAVE10 takes 10% off the subtotal, the "
               "discounted total is what checkout charges, and unknown codes are refused.",
    "user_stories": [
        {"id": "US-1", "as_a": "shopper", "i_want": "to enter a coupon code in my cart",
         "so_that": "I pay less when I have a discount code"},
        {"id": "US-2", "as_a": "shop owner", "i_want": "only real coupon codes to give a discount",
         "so_that": "nobody gets a discount by guessing"},
    ],
    "acceptance_criteria": [
        {"id": "AC-1", "story_id": "US-1", "given": "a cart with 2 Desk Lamps (subtotal ₹3,798.00)",
         "when": "the coupon SAVE10 is applied",
         "then": "the discount is ₹379.80, the total is ₹3,418.20 and the cart shows coupon SAVE10"},
        {"id": "AC-2", "story_id": "US-1", "given": "the same cart", "when": "the code is typed as '  save10 '",
         "then": "it is accepted like SAVE10 and the total is ₹3,418.20"},
        {"id": "AC-3", "story_id": "US-2", "given": "the same cart", "when": "the code FREE100 is applied",
         "then": "the request fails with status 400 mentioning 'unknown coupon', no coupon is set and "
                 "the total stays ₹3,798.00"},
        {"id": "AC-4", "story_id": "US-1", "given": "a cart with SAVE10 applied", "when": "the shopper checks out",
         "then": "the order total is ₹3,418.20, the order records coupon SAVE10 and the new cart has no coupon"},
        {"id": "AC-5", "story_id": "US-1", "given": "the shop page", "when": "it is loaded",
         "then": "the cart has a coupon box (id coupon-code) and an Apply button (id apply-coupon)"},
    ],
    "open_questions": ["Can two coupons stack? Assumed no: applying a coupon replaces the previous one."],
    "out_of_scope": ["Coupon expiry dates and usage limits", "Managing coupons from an admin page"],
}

DESIGN = {
    "approach": "Add a coupons module that maps codes to a percentage off. The cart stores the applied "
                "coupon and computes the discount in whole paise; total() becomes subtotal minus discount. "
                "A new POST /api/cart/coupon endpoint applies a code, and the page gets a coupon box.",
    "files": [
        {"path": "shoplite/coupons.py", "change": "create", "purpose": "known coupon codes, CouponError, lookup()"},
        {"path": "shoplite/cart.py", "change": "modify", "purpose": "store the coupon; discount(); total() after discount"},
        {"path": "shoplite/main.py", "change": "modify", "purpose": "POST /api/cart/coupon; coupon and discount in cart JSON"},
        {"path": "shoplite/static/index.html", "change": "modify", "purpose": "coupon box and discount line in the cart"},
    ],
    "test_files": ["tests/test_coupons.py"],
    "interfaces": [
        {"name": "lookup", "signature": "def lookup(code: str) -> tuple[str, int]",
         "description": "Canonical code and percent off; ignores case and spaces; raises CouponError if unknown"},
        {"name": "Cart.apply_coupon", "signature": "def apply_coupon(self, code: str) -> None",
         "description": "Apply a coupon, replacing any earlier one; the cart is unchanged if the code is unknown"},
        {"name": "Cart.discount", "signature": "def discount(self) -> int",
         "description": "Discount in paise: subtotal × percent ÷ 100, rounded down"},
        {"name": "POST /api/cart/coupon", "signature": "body {code: str} -> cart JSON",
         "description": "400 with the CouponError message for unknown codes"},
    ],
    "risks": ["Rounding: the discount is rounded down to whole paise, so the shop never over-discounts"],
    "adr": {"title": "Coupons as a percentage stored on the cart",
            "decision": "The cart keeps the applied code and percentage; the discount is computed, not stored",
            "alternatives": ["Store the discount amount when the coupon is applied"],
            "consequences": "Adding items after applying a coupon updates the discount automatically"},
}

TESTS = '''from fastapi.testclient import TestClient

from shoplite.main import create_app


def cart_with_two_lamps() -> TestClient:
    client = TestClient(create_app())
    client.post("/api/cart/items", json={"product_id": "lamp", "quantity": 2})  # 2 x Rs 1,899.00
    return client


def test_save10_takes_ten_percent_off():
    cart = cart_with_two_lamps().post("/api/cart/coupon", json={"code": "SAVE10"}).json()
    assert cart["subtotal"] == 379800
    assert cart["discount"] == 37980
    assert cart["total"] == 341820
    assert cart["coupon"] == "SAVE10"


def test_coupon_codes_ignore_case_and_spaces():
    r = cart_with_two_lamps().post("/api/cart/coupon", json={"code": "  save10 "})
    assert r.status_code == 200
    assert r.json()["total"] == 341820


def test_unknown_coupon_is_rejected_and_total_unchanged():
    client = cart_with_two_lamps()
    r = client.post("/api/cart/coupon", json={"code": "FREE100"})
    assert r.status_code == 400
    assert "unknown coupon" in r.json()["detail"]
    cart = client.get("/api/cart").json()
    assert cart["total"] == 379800
    assert cart["coupon"] is None


def test_checkout_charges_the_discounted_total():
    client = cart_with_two_lamps()
    client.post("/api/cart/coupon", json={"code": "SAVE10"})
    order = client.post("/api/checkout").json()
    assert order["total"] == 341820
    assert order["coupon"] == "SAVE10"
    assert client.get("/api/cart").json()["coupon"] is None


def test_cart_page_has_a_coupon_box():
    page = TestClient(create_app()).get("/").text
    assert 'id="coupon-code"' in page
    assert 'id="apply-coupon"' in page
'''

COVERAGE_MAP = [
    {"ac_id": "AC-1", "tests": ["test_save10_takes_ten_percent_off"]},
    {"ac_id": "AC-2", "tests": ["test_coupon_codes_ignore_case_and_spaces"]},
    {"ac_id": "AC-3", "tests": ["test_unknown_coupon_is_rejected_and_total_unchanged"]},
    {"ac_id": "AC-4", "tests": ["test_checkout_charges_the_discounted_total"]},
    {"ac_id": "AC-5", "tests": ["test_cart_page_has_a_coupon_box"]},
]

_COUPONS = '''"""Discount coupons. A coupon takes a whole-number percentage off the cart subtotal."""

COUPONS: dict[str, int] = {"SAVE10": 10}


class CouponError(ValueError):
    """A coupon code the shop doesn't know."""


def lookup(code: str) -> tuple[str, int]:
    """The coupon's canonical code and its percentage off."""
    key = {normalise}
    if key not in COUPONS:
        raise CouponError(f"unknown coupon {code.strip()!r}")
    return key, COUPONS[key]
'''
COUPONS_WRONG = _COUPONS.replace("{normalise}", "code.strip()")  # forgets letter case: AC-2 fails
COUPONS_RIGHT = _COUPONS.replace("{normalise}", "code.strip().upper()")


def _patch(text: str, *edits: tuple[str, str]) -> str:
    for old, new in edits:
        if text.count(old) != 1:
            raise RuntimeError(f"fake replay is out of date with the ShopLite example: {old[:60]!r}")
        text = text.replace(old, new)
    return text


def cart_py(base: str) -> str:
    return _patch(
        base,
        ("from shoplite.catalog import Product, find\n",
         "from shoplite.catalog import Product, find\nfrom shoplite.coupons import lookup\n"),
        ("    lines: dict[str, Line] = field(default_factory=dict)\n",
         "    lines: dict[str, Line] = field(default_factory=dict)\n"
         "    coupon: str | None = None\n"
         "    percent_off: int = 0\n"),
        ("    def total(self) -> int:\n        return self.subtotal()\n",
         "    def apply_coupon(self, code: str) -> None:\n"
         "        self.coupon, self.percent_off = lookup(code)\n\n"
         "    def discount(self) -> int:\n"
         "        return self.subtotal() * self.percent_off // 100\n\n"
         "    def total(self) -> int:\n        return self.subtotal() - self.discount()\n"),
        ("    def clear(self) -> None:\n        self.lines.clear()\n",
         "    def clear(self) -> None:\n        self.lines.clear()\n"
         "        self.coupon, self.percent_off = None, 0\n"),
    )


def main_py(base: str) -> str:
    return _patch(
        base,
        ("from shoplite.catalog import PRODUCTS\n",
         "from shoplite.catalog import PRODUCTS\nfrom shoplite.coupons import CouponError\n"),
        ("    quantity: int = 1\n",
         "    quantity: int = 1\n\n\nclass CouponIn(BaseModel):\n    code: str\n"),
        ('        "total": cart.total(),\n',
         '        "coupon": cart.coupon,\n        "discount": cart.discount(),\n        "total": cart.total(),\n'),
        ('    @app.post("/api/checkout")\n',
         '    @app.post("/api/cart/coupon")\n'
         "    def apply_coupon(body: CouponIn) -> dict:\n"
         "        try:\n"
         "            cart.apply_coupon(body.code)\n"
         "        except CouponError as exc:\n"
         "            raise HTTPException(400, str(exc)) from exc\n"
         "        return cart_json(cart)\n\n"
         '    @app.post("/api/checkout")\n'),
    )


def index_html(base: str) -> str:
    return _patch(
        base,
        ('    <div class="row total">',
         '    <form class="row" id="coupon-form" style="gap:8px">\n'
         '      <input id="coupon-code" placeholder="Coupon code" aria-label="Coupon code" autocomplete="off">\n'
         '      <button type="submit" id="apply-coupon" class="ghost" style="padding:8px 12px">Apply</button>\n'
         '    </form>\n'
         '    <div class="row" id="discount-row" style="display:none"><span class="muted">Discount '
         '<span id="coupon-name"></span></span><span id="discount"></span></div>\n'
         '    <div class="row total">'),
        ('    $("#total").textContent = money(cart.total);\n',
         '    $("#total").textContent = money(cart.total);\n'
         '    $("#discount-row").style.display = cart.discount ? "" : "none";\n'
         '    $("#discount").textContent = "−" + money(cart.discount);\n'
         '    $("#coupon-name").textContent = cart.coupon ? `(${cart.coupon})` : "";\n'),
        ("  load();\n</script>",
         '  $("#coupon-form").addEventListener("submit", async (e) => {\n'
         "    e.preventDefault();\n"
         "    try {\n"
         '      const cart = await api("/api/cart/coupon", {method: "POST", body: JSON.stringify({code: $("#coupon-code").value})});\n'
         "      renderCart(cart);\n"
         '      say(`Coupon ${cart.coupon} applied: you save ${money(cart.discount)}.`, "ok");\n'
         '    } catch (err) { say(err.message, "bad"); }\n'
         "  });\n\n"
         "  load();\n</script>"),
    )


def build(base: dict[str, str], iteration: int) -> dict:
    return {"files": [
        {"path": "shoplite/coupons.py", "content": COUPONS_WRONG if iteration == 0 else COUPONS_RIGHT},
        {"path": "shoplite/cart.py", "content": cart_py(base["shoplite/cart.py"])},
        {"path": "shoplite/main.py", "content": main_py(base["shoplite/main.py"])},
        {"path": "shoplite/static/index.html", "content": index_html(base["shoplite/static/index.html"])},
    ], "notes": "first attempt" if iteration == 0 else "coupon codes now ignore letter case"}


REVIEW = {"verdict": "approve", "comments": [
    {"file": "shoplite/coupons.py", "line": 3, "severity": "suggestion",
     "comment": "Coupons are hard-coded. Fine for one code; move them to config or a database before "
                "adding more."},
    {"file": "shoplite/payments.py", "line": 12, "severity": "blocking",
     "comment": "This comment points at a file that isn't in the diff, so it must be dropped.",
     "scenario": "apply SAVE10 twice on a file that doesn't exist"},
]}
