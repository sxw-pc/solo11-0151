"""Nominal single-point verdict for the existing workshop intake."""

from .store import log


def classify(db, sample):
    error = abs(sample["display"] - sample["nominal_display"])
    state = "calibrated" if error <= sample["allowed_difference"] else "held"
    log(db, "bench", sample["meter"], state, "nominal display tolerance", sample["id"])
    return {"meter": sample["meter"], "state": state}
