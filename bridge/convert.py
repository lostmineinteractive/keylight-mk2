"""Value ranges and unit conversion for the light.

The light speaks mireds, not Kelvin, and a higher mired value is a WARMER
light: 143 is the coolest setting (~7000K), 344 the warmest (~2900K).
"""

from __future__ import annotations

MIRED_MIN, MIRED_MAX = 143, 344
KELVIN_MIN, KELVIN_MAX = 2900, 7000
BRIGHTNESS_MIN, BRIGHTNESS_MAX = 0, 100


def _clamp(value: float, low: int, high: int) -> int:
    return max(low, min(high, round(value)))


def clamp_brightness(value: float) -> int:
    return _clamp(value, BRIGHTNESS_MIN, BRIGHTNESS_MAX)


def clamp_mired(value: float) -> int:
    return _clamp(value, MIRED_MIN, MIRED_MAX)


def kelvin_to_mired(kelvin: float) -> int:
    kelvin = max(KELVIN_MIN, min(KELVIN_MAX, kelvin))
    return clamp_mired(1_000_000 / kelvin)


def mired_to_kelvin(mired: float) -> int:
    """Kelvin for display, rounded to the 50K steps Control Center shows."""
    return _clamp(1_000_000 / mired / 50, KELVIN_MIN // 50, KELVIN_MAX // 50) * 50
