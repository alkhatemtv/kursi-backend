"""Seat pricing, in integer minor units only (spec, Decision 5).

There are exactly two places a seat's price can come from, and this module is
the only thing that knows the precedence:

    1. `performance_seats.price_override_minor` - a per-seat exception
    2. `performance_categories.amount_minor`    - the per-performance price for
                                                  the seat's category key

If neither exists the seat is not sellable and `PricingUnavailable` is raised.
Falling back to zero would silently give a paid seat away, which is the worst
available outcome; the layout or the publish call has to be fixed instead.

`MinorAmount` in the ORM already refuses a float or Decimal at bind time, but
that error arrives from deep inside a flush. Validating the caller's price map
here means a mistyped price is rejected at the API boundary with a message that
names the category.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engine_models import PerformanceCategory, PerformanceSeat
from app.engine_services.errors import PricingUnavailable, ValidationError

#: The platform's home currency. Kuwaiti dinar has three minor digits, so
#: KWD 5.500 is 5500 fils - which is precisely why money is never a float here.
DEFAULT_CURRENCY = "KWD"


def _validate_amount(category_key: str, amount: Any) -> int:
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise ValidationError(
            f"price for category {category_key!r} must be an integer number of "
            f"minor units (KWD 5.500 -> 5500), got "
            f"{type(amount).__name__}: {amount!r}",
            category_key=category_key,
        )
    if amount < 0:
        raise ValidationError(
            f"price for category {category_key!r} must not be negative",
            category_key=category_key,
        )
    return amount


def normalize_prices(
    prices: Mapping[str, Any] | None, default_currency: str = DEFAULT_CURRENCY
) -> dict[str, tuple[int, str]]:
    """Accept `{key: 5500}` or `{key: {"amount_minor": 5500, "currency": "KWD"}}`.

    Returns `{category_key: (amount_minor, currency)}`.
    """
    normalized: dict[str, tuple[int, str]] = {}
    for key, value in (prices or {}).items():
        if isinstance(value, Mapping):
            amount = value.get("amount_minor", value.get("amount"))
            currency = value.get("currency") or default_currency
        else:
            amount, currency = value, default_currency
        if not isinstance(currency, str) or len(currency) != 3:
            raise ValidationError(
                f"currency for category {key!r} must be a 3-letter ISO code, "
                f"got {currency!r}",
                category_key=key,
            )
        normalized[str(key)] = (_validate_amount(str(key), amount), currency.upper())
    return normalized


def load_performance_categories(
    session: Session, performance_id: int
) -> dict[str, PerformanceCategory]:
    rows = (
        session.execute(
            select(PerformanceCategory).where(
                PerformanceCategory.performance_id == performance_id
            )
        )
        .scalars()
        .all()
    )
    return {row.category_key: row for row in rows}


def price_for_seat(
    seat: PerformanceSeat, categories: Mapping[str, PerformanceCategory]
) -> tuple[int, str]:
    """(amount_minor, currency) for one seat. Override beats category."""
    if seat.price_override_minor is not None:
        return seat.price_override_minor, seat.currency or DEFAULT_CURRENCY

    category = categories.get(seat.category_key) if seat.category_key else None
    if category is None:
        raise PricingUnavailable(
            f"seat {seat.seat_uid!r} has no price: category "
            f"{seat.category_key!r} is not priced for this performance and the "
            f"seat has no price_override_minor",
            seat_uid=seat.seat_uid,
            category_key=seat.category_key,
        )
    return category.amount_minor, category.currency


def price_seats(
    seats: list[PerformanceSeat], categories: Mapping[str, PerformanceCategory]
) -> tuple[dict[int, int], str]:
    """Price a whole basket.

    Returns `({seat_id: amount_minor}, currency)`. One order carries one
    currency column, so a basket that mixes currencies is rejected rather than
    silently summed.
    """
    amounts: dict[int, int] = {}
    currencies: set[str] = set()
    for seat in seats:
        amount, currency = price_for_seat(seat, categories)
        amounts[seat.id] = amount
        currencies.add(currency)

    if len(currencies) > 1:
        raise ValidationError(
            f"seats span multiple currencies ({', '.join(sorted(currencies))}); "
            f"an order carries exactly one",
            currencies=sorted(currencies),
        )
    return amounts, (currencies.pop() if currencies else DEFAULT_CURRENCY)
