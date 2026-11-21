"""Synthetic meter response model for bench re-verification.

The instrument is one fixed affine response for a whole verification round:

    display = clamp(floor(slope * true_load + offset), 0, max_display)

A run may use only one (slope, offset) pair for the entire load range; it is
not legal to pick different parameters for the low and high segments. All
data here is synthetic and used solely for offline evidence work.
"""

import math


def response(slope, offset, true_load, max_display=9):
    """Return the displayed integer for a true integer-unit load."""
    raw = math.floor(slope * true_load + offset)
    return min(max_display, max(0, raw))


def build_curve(slope, offset, actual_low, actual_high, max_display=9):
    """Full response line as a {true_load: display} mapping."""
    return {
        load: response(slope, offset, load, max_display)
        for load in range(actual_low, actual_high + 1)
    }


def candidate_curves(case):
    """Enumerate every full instrument curve allowed by the synthetic case."""
    max_display = case["max_display"]
    low, high = case["actual_range"]
    curves = []
    for slope in case["candidate_slopes"]:
        for offset in case["candidate_offsets"]:
            curves.append(
                {
                    "slope": slope,
                    "offset": offset,
                    "responses": build_curve(slope, offset, low, high, max_display),
                }
            )
    return curves


def curve_matches(curve, load, display):
    """Whether a full curve produces ``display`` at ``load``.

    Saturation is a real observation: endpoint-only matches (the display only
    happens at the clipped end of the curve) must be retained, never skipped.
    """
    return curve["responses"].get(load) == display


def filter_curves(curves, observations):
    """Keep curves consistent with every (load, display) observation."""
    kept = []
    for curve in curves:
        if all(curve_matches(curve, load, display) for load, display in observations):
            kept.append(curve)
    return kept


def curve_label(curve):
    return {"slope": curve["slope"], "offset": curve["offset"]}
