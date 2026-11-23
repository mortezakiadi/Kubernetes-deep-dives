"""The building blocks of the task bank: four small modules of a shop library, and two bugs that can be injected into
each. A bug is a one-line change; the issue text gives a concrete wrong output; the hidden test checks the fix."""

PRICING = '''"""Prices and discounts."""


def apply_discount(price, percent):
    """Price after a percentage discount, rounded to cents."""
    return round(price * (1 - percent / 100), 2)


def bulk_price(unit, qty):
    """Ten or more items get 10% off."""
    if qty >= 10:
        return round(unit * qty * 0.9, 2)
    return round(unit * qty, 2)
'''

CART = '''"""Cart totals."""


def subtotal(items):
    """items: a list of (unit_price, quantity)."""
    return round(sum(price * qty for price, qty in items), 2)


def with_tax(amount, rate):
    """Amount plus tax at `rate` percent, rounded to cents."""
    return round(amount * (1 + rate / 100), 2)
'''

STOCK = '''"""Stock levels."""


def remove(stock, item, n):
    """Take n of item out of stock; refuse if there are not enough."""
    have = stock.get(item, 0)
    if n > have:
        raise ValueError("not enough " + item)
    return {**stock, item: have - n}


def low_items(stock, threshold):
    """Items whose level is at or below the threshold, sorted."""
    return sorted(k for k, v in stock.items() if v <= threshold)
'''

TEXT = '''"""Text helpers for product pages."""


def slugify(name):
    """'Blue Pen  XL' -> 'blue-pen-xl'."""
    return "-".join(name.lower().split())


def truncate(text, n):
    """At most n characters; end with '...' when something was cut."""
    if len(text) <= n:
        return text
    return text[: n - 3] + "..."
'''

# module path -> (source, module import name, visible regression test body, bugs)
# bug: (function, correct text, buggy text, issue sentence, hidden test body)
MODULES = {
    "shop/pricing.py": (PRICING, "shop.pricing",
                        "self.assertEqual(apply_discount(50, 0), 50.0)\n        self.assertEqual(bulk_price(3, 1), 3)", [
        ("apply_discount", "(1 - percent / 100)", "(1 - percent / 10)",
         "apply_discount(100, 15) returns -50.0; it should return 85.0.",
         "self.assertEqual(apply_discount(100, 15), 85.0)\n        self.assertEqual(apply_discount(80, 50), 40.0)"),
        ("bulk_price", "if qty >= 10:", "if qty > 10:",
         "bulk_price(2, 10) returns 20; ten items should already get the bulk price, 18.0.",
         "self.assertEqual(bulk_price(2, 10), 18.0)\n        self.assertEqual(bulk_price(2, 9), 18)"),
    ]),
    "shop/cart.py": (CART, "shop.cart",
                     "self.assertEqual(subtotal([]), 0)\n        self.assertEqual(with_tax(10, 0), 10)", [
        ("subtotal", "sum(price * qty for price, qty in items)", "sum(price for price, qty in items)",
         "subtotal([(2.5, 4)]) returns 2.5; it should return 10.0.",
         "self.assertEqual(subtotal([(2.5, 4)]), 10.0)\n        self.assertEqual(subtotal([(1, 2), (3, 1)]), 5)"),
        ("with_tax", "(1 + rate / 100)", "(1 + rate)",
         "with_tax(100, 8) returns 900; it should return 108.0.",
         "self.assertEqual(with_tax(100, 8), 108.0)\n        self.assertEqual(with_tax(50, 20), 60.0)"),
    ]),
    "shop/stock.py": (STOCK, "shop.stock",
                      "self.assertEqual(remove({'pen': 5}, 'pen', 1), {'pen': 4})", [
        ("remove", "if n > have:", "if n >= have:",
         "remove({'pen': 3}, 'pen', 3) raises ValueError; taking the last three pens must work and leave 0.",
         "self.assertEqual(remove({'pen': 3}, 'pen', 3), {'pen': 0})\n        with self.assertRaises(ValueError):\n            remove({'pen': 1}, 'pen', 2)"),
        ("low_items", "if v <= threshold", "if v < threshold",
         "low_items({'a': 2, 'b': 5}, 2) returns []; 'a' is at the threshold and should be listed.",
         "self.assertEqual(low_items({'a': 2, 'b': 5}, 2), ['a'])\n        self.assertEqual(low_items({'z': 0, 'y': 1}, 1), ['y', 'z'])"),
    ]),
    "shop/text.py": (TEXT, "shop.text",
                     "self.assertEqual(truncate('abc', 5), 'abc')", [
        ("slugify", '"-".join(name.lower().split())', '"-".join(name.split())',
         "slugify('Blue Pen') returns 'Blue-Pen'; slugs must be lower case: 'blue-pen'.",
         "self.assertEqual(slugify('Blue Pen'), 'blue-pen')\n        self.assertEqual(slugify('  A  b '), 'a-b')"),
        ("truncate", "text[: n - 3]", "text[: n]",
         "truncate('abcdefgh', 6) returns 'abcdef...' (9 characters); the result must be at most 6 characters: 'abc...'.",
         "self.assertEqual(truncate('abcdefgh', 6), 'abc...')\n        self.assertEqual(len(truncate('x' * 20, 10)), 10)"),
    ]),
}
