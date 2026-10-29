"""Shared validation utilities."""

from decimal import Decimal

ZERO = Decimal("0")


def percent_difference(difference: Decimal, base: Decimal) -> Decimal:
    """Calculate percentage difference between two amounts.
    
    Args:
        difference: Absolute or signed difference
        base: Denominator (base amount)
        
    Returns:
        Percentage as a Decimal, or Decimal("100") if base is zero and difference is nonzero.
    """
    difference = difference.copy_abs()
    if not base:
        return Decimal("100") if difference else ZERO
    return (difference / base.copy_abs() * Decimal("100")).quantize(Decimal("0.0001"))
