"""Reference test files for the Test Generation golden cases: what a good answer looks like.

Used by tests/agents/test_test_gen.py to prove each case's scoring accepts good tests.
"""

REFERENCE_TESTS = {
    "slugify": """\
import pytest

from textkit.slug import slugify


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Hello, World!", "hello-world"),
        ("  --Already--Slugged--  ", "already-slugged"),
        ("Crème Brûlée", "creme-brulee"),
        ("", "untitled"),
        ("!!!", "untitled"),
    ],
)
def test_slugify(title: str, expected: str) -> None:
    assert slugify(title) == expected


def test_truncation_does_not_end_with_a_dash() -> None:
    assert slugify("abc def", max_length=4) == "abc"
    assert slugify("abcdef", max_length=3) == "abc"


def test_max_length_must_be_positive() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        slugify("x", max_length=0)
""",
    "split_bill": """\
from decimal import Decimal

import pytest

from billing.split import split_bill


@pytest.mark.parametrize(
    ("total", "people", "expected"),
    [
        ("10.00", 2, ["5.00", "5.00"]),
        ("10.00", 3, ["3.34", "3.33", "3.33"]),
        ("0.05", 3, ["0.02", "0.02", "0.01"]),
        ("0", 2, ["0.00", "0.00"]),
        ("7.50", 1, ["7.50"]),
    ],
)
def test_shares(total: str, people: int, expected: list[str]) -> None:
    shares = split_bill(Decimal(total), people)
    assert shares == [Decimal(e) for e in expected]
    assert sum(shares) == Decimal(total)


def test_bad_input() -> None:
    with pytest.raises(ValueError, match="people"):
        split_bill(Decimal("1"), 0)
    with pytest.raises(ValueError, match="negative"):
        split_bill(Decimal("-0.01"), 2)
""",
    "inventory": """\
import pytest

from shop.inventory import Inventory, OutOfStockError


@pytest.fixture
def inventory() -> Inventory:
    inv = Inventory()
    inv.add("pen", 3)
    inv.add("cup", 1)
    return inv


def test_remove_some_and_all(inventory: Inventory) -> None:
    inventory.remove("pen", 2)
    assert inventory.count("pen") == 1
    inventory.remove("pen", 1)
    assert inventory.count("pen") == 0
    assert inventory.skus() == ["cup"]


def test_skus_sorted(inventory: Inventory) -> None:
    inventory.add("bag", 1)
    assert inventory.skus() == ["bag", "cup", "pen"]


def test_out_of_stock(inventory: Inventory) -> None:
    with pytest.raises(OutOfStockError, match="pen: have 3, need 4"):
        inventory.remove("pen", 4)
    assert inventory.count("pen") == 3


@pytest.mark.parametrize("quantity", [0, -1])
def test_quantity_must_be_positive(inventory: Inventory, quantity: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        inventory.add("pen", quantity)
    with pytest.raises(ValueError, match="positive"):
        inventory.remove("pen", quantity)
""",
    "business_days": """\
from datetime import date

import pytest

from delivery.calendar import add_business_days

MONDAY = date(2026, 10, 5)
FRIDAY = date(2026, 10, 9)


@pytest.mark.parametrize(
    ("start", "days", "expected"),
    [
        (MONDAY, 0, MONDAY),
        (MONDAY, 1, date(2026, 10, 6)),
        (MONDAY, 5, date(2026, 10, 12)),
        (FRIDAY, 1, date(2026, 10, 12)),
        (date(2026, 10, 10), 1, date(2026, 10, 12)),
    ],
)
def test_skips_weekends(start: date, days: int, expected: date) -> None:
    assert add_business_days(start, days) == expected


def test_skips_holidays() -> None:
    holiday = frozenset({date(2026, 10, 12)})
    assert add_business_days(FRIDAY, 1, holiday) == date(2026, 10, 13)


def test_negative_days() -> None:
    with pytest.raises(ValueError, match="zero or more"):
        add_business_days(MONDAY, -1)
""",
    "lru_cache": """\
import pytest

from cachekit.lru import LRUCache


def test_get_and_default() -> None:
    cache = LRUCache(2)
    cache.put("a", 1)
    assert cache.get("a") == 1
    assert cache.get("missing") is None
    assert cache.get("missing", 0) == 0


def test_evicts_least_recently_used() -> None:
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1  # "a" is now the most recent
    cache.put("c", 3)
    assert cache.get("b") is None
    assert cache.get("a") == 1
    assert cache.get("c") == 3
    assert len(cache) == 2


def test_put_existing_key_updates_and_refreshes() -> None:
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("a", 10)
    cache.put("c", 3)
    assert cache.get("a") == 10
    assert cache.get("b") is None


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        LRUCache(0)
""",
}
