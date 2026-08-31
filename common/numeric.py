"""Numeric guards.

The Garmin SpO2 exports contain NaN, and NaN does not raise. It propagates
silently through sum() and mean, and str(nan) is the string 'nan', which is
then posted to a NUMBER data element. Measured over 93 participant files,
77 of them (83%) contain at least one NaN, so this is the common case and
not an edge case.
"""

import math


def is_finite_number(value):
    """True for a real, finite int or float. False for NaN, inf, and non-numbers."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def clean_readings(readings, predicate=None):
    """Drop (timestamp, value) pairs whose value is not a finite number.

    predicate: optional extra test applied to values that are already finite,
    for example lambda v: v > 0 to drop the Garmin zero-fill on heart rate.
    """
    out = []
    for ts, value in readings:
        if not is_finite_number(value):
            continue
        if predicate is not None and not predicate(value):
            continue
        out.append((ts, value))
    return out


def safe_round(value, digits):
    """round() that returns None instead of a NaN or infinity."""
    if not is_finite_number(value):
        return None
    return round(value, digits)
