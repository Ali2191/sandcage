from decimal import Decimal

import pytest

from invoice_calc import LineItem, apply_discount, invoice_total, subtotal


def test_line_item_total() -> None:
    assert LineItem("Hosting", Decimal("15.50"), 3).total == Decimal("46.50")


def test_subtotal() -> None:
    items = [LineItem("a", Decimal("10.00"), 2), LineItem("b", Decimal("5.25"), 1)]
    assert subtotal(items) == Decimal("25.25")


def test_invoice_total_without_discount_adds_tax() -> None:
    assert invoice_total([LineItem("a", Decimal("100.00"), 1)]) == Decimal("108.00")


def test_discount_is_a_percentage() -> None:
    assert apply_discount(Decimal("200.00"), Decimal("10")) == Decimal("180.00")


def test_invalid_discount_is_rejected() -> None:
    with pytest.raises(ValueError, match="between 0 and 100"):
        apply_discount(Decimal("10.00"), Decimal("101"))
