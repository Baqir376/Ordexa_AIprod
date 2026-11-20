"""Deterministic decimal money arithmetic (SRS PRICE-005). Never use float for money."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

TWO = Decimal("0.01")
QTY = Decimal("0.001")


def money(value: Decimal | str | int) -> Decimal:
    return Decimal(str(value)).quantize(TWO, rounding=ROUND_HALF_UP)


def qty(value: Decimal | str | int) -> Decimal:
    return Decimal(str(value)).quantize(QTY, rounding=ROUND_HALF_UP)


def line_total(quantity: Decimal, unit_price: Decimal, discount: Decimal = Decimal(0)) -> Decimal:
    return money(qty(quantity) * money(unit_price) - money(discount))
