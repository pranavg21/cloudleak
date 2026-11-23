"""Exact decimal handling.

DuckDB computes in DECIMAL(38,12), Python receives decimal.Decimal, and JSON
receives strings. Parquet inputs can already contain binary floating amounts.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from functools import wraps
from typing import ParamSpec, TypeVar

SCALE = 12
SQL_MONEY = f"DECIMAL(38,{SCALE})"
ZERO = Decimal(0)
P = ParamSpec("P")
T = TypeVar("T")


def money_context(func: Callable[P, T]) -> Callable[P, T]:
    """Preserve DECIMAL(38,12) sums and intermediate quantity/rate products."""
    @wraps(func)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        with localcontext() as ctx:
            ctx.prec = max(ctx.prec, 128)
            return func(*args, **kwargs)
    return wrapped


def to_decimal(value: object) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value)
    raise TypeError(f"refusing to convert {type(value).__name__} to money")


def fmt(value: Decimal | None) -> str | None:
    """Canonical string form: no exponent, trailing zeros trimmed, at least 2 dp."""
    if value is None:
        return None
    value = to_decimal(value)
    if value == 0:
        return "0.00"
    text = format(value, "f")
    if "." not in text:
        return text + ".00"
    whole, frac = text.split(".")
    frac = frac.rstrip("0")
    if len(frac) < 2:
        frac = frac.ljust(2, "0")
    return f"{whole}.{frac}"


def display(value: Decimal | None, currency: str | None = None, places: int = 2) -> str:
    """Human display, rounded half-even. Never used for arithmetic."""
    if value is None:
        return "n/a"
    value = to_decimal(value)
    q = Decimal(1).scaleb(-places)
    with localcontext() as ctx:
        ctx.prec = max(80, value.adjusted() + places + 2)
        rounded = value.quantize(q, rounding=ROUND_HALF_EVEN)
    text = f"{rounded:,.{places}f}"
    if text.startswith("-0.") and rounded == 0:
        text = text[1:]
    return f"{text} {currency}" if currency else text


def pct(part: Decimal, whole: Decimal) -> str:
    if whole == 0:
        return "n/a"
    return f"{(to_decimal(part) / to_decimal(whole) * 100).quantize(Decimal('0.1'), rounding=ROUND_HALF_EVEN)}%"
