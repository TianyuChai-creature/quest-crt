"""Joint radii in meters; wire NaN represents an unavailable radius."""

import math
from collections.abc import Sequence


def encode_joint_radii(radii: Sequence[float | None] | None) -> list[float]:
    if radii is None:
        return [math.nan] * 21
    if len(radii) != 21:
        raise ValueError("hand radii must have length 21")
    values = [math.nan if radius is None else float(radius) for radius in radii]
    if any(
        radius is not None and (not math.isfinite(value) or value < 0)
        for radius, value in zip(radii, values)
    ):
        raise ValueError("joint radii must be finite and nonnegative or None")
    return values


def decode_joint_radii(values: Sequence[float]) -> list[float | None]:
    radii = [None if math.isnan(value) else float(value) for value in values]
    encode_joint_radii(radii)
    return radii
