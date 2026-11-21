"""Range certification for bench re-verification rounds.

Unlike the historical single-point verdict (``bench_result.classify``), a
range certificate states, per display value, which corrected true load may be
accepted. A correction is issued only when it holds for *every* admissible
instrument curve within the tolerance; saturated displays whose true load
cannot be fixed are returned as counterexamples plus uncovered ranges rather
than interpolated from archived readings.
"""

import json
from collections import defaultdict

from .bench_curve import candidate_curves, curve_label, filter_curves
from .store import log


def _round_evidence(db, round_id):
    """Evidence rows of one verification round, oldest first."""
    return db.execute(
        "SELECT * FROM bench_evidence WHERE round_id=? ORDER BY rowid",
        (round_id,),
    ).fetchall()


def _compatible_curves(case, observations):
    return filter_curves(candidate_curves(case), observations)


def _key(label):
    return (label["slope"], label["offset"])


def _label(key):
    return {"slope": key[0], "offset": key[1]}


def _display_rows(curves, actual_range, max_display):
    """Per-display possible true loads over the admissible curves.

    Every full curve is considered, so a display only reached at the
    saturation endpoint still reports every load that can produce it.
    """
    by_display = defaultdict(list)
    explaining = defaultdict(list)
    for curve in curves:
        label = _key(curve_label(curve))
        for load, display in curve["responses"].items():
            by_display[display].append(load)
            explaining[display].append(label)
    rows = []
    for display in range(0, max_display + 1):
        rows.append(
            {
                "display": display,
                "possible_loads": sorted(set(by_display.get(display, ()))),
                "explaining_curves": [
                    _label(key) for key in sorted(set(explaining.get(display, ())))
                ],
            }
        )
    return rows


def _uncovered_segments(rows):
    """Contiguous display segments with no certifiable correction."""
    segments = []
    current = None
    for row in rows:
        if row["correction"] is None:
            if current and row["display"] == current["to"] + 1:
                current["to"] = row["display"]
            else:
                current = {"from": row["display"], "to": row["display"]}
                segments.append(current)
        else:
            current = None
    return segments


def build_certificate(case, observations, tolerance=0.5):
    """Pure certification logic over fixed-round observations.

    Returns a certificate mapping each display to either a certifiable
    corrected load or the counterexample loads that block signing, plus the
    still-compatible full instrument curves and uncovered display segments.
    """
    curves = _compatible_curves(case, observations)
    rows = _display_rows(curves, case["actual_range"], case["max_display"])
    corrections = {}
    for row in rows:
        loads = row["possible_loads"]
        correction = loads[0] if len(loads) == 1 else None
        if correction is not None and tolerance is not None:
            if any(abs(load - correction) > tolerance for load in loads):
                correction = None
        row["correction"] = correction
        row["certifiable"] = correction is not None
        if correction is not None:
            corrections[row["display"]] = correction
    saturated = case["max_display"]
    segments = _uncovered_segments(rows)
    high_end = next(row for row in rows if row["display"] == saturated)
    state = "signed" if high_end["certifiable"] else "partial"
    if not curves:
        state = "contradictory"
    return {
        "state": state,
        "actual_range": list(case["actual_range"]),
        "max_display": case["max_display"],
        "tolerance": tolerance,
        "observations": [
            {"load": load, "display": display} for load, display in observations
        ],
        "admissible_curve_count": len(curves),
        "admissible_curves": [curve_label(curve) for curve in curves],
        "corrections": corrections,
        "displays": rows,
        "counterexamples": [
            {
                "display": row["display"],
                "possible_loads": row["possible_loads"],
                "explaining_curves": row["explaining_curves"],
            }
            for row in rows
            if not row["certifiable"]
        ],
        "uncovered_segments": segments,
        "saturated_display": {
            "display": saturated,
            "possible_loads": high_end["possible_loads"],
            "certifiable": high_end["certifiable"],
        },
    }


def certify_round(db, round_id, case, tolerance=0.5, meter=None):
    """Issue (or refresh) the certificate for one recorded round."""
    rows = _round_evidence(db, round_id)
    if not rows:
        raise ValueError(f"no evidence recorded for round {round_id}")
    observations = [(row["true_load"], row["display"]) for row in rows]
    certificate = build_certificate(case, observations, tolerance)
    meter = meter or rows[0]["meter"]
    round_key = f"{meter}:{round_id}"
    state = certificate["state"]
    log(
        db,
        "bench-cert",
        round_id,
        state,
        f"range certificate: {len(certificate['corrections'])} displays certifiable",
    )
    db.execute(
        "INSERT INTO bench_certificate(round_id,meter,state,tolerance,corrections_json,"
        "counterexamples_json,segments_json,curves_json,certificate_json) "
        "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(round_id) DO UPDATE SET "
        "meter=excluded.meter,state=excluded.state,tolerance=excluded.tolerance,"
        "corrections_json=excluded.corrections_json,"
        "counterexamples_json=excluded.counterexamples_json,"
        "segments_json=excluded.segments_json,curves_json=excluded.curves_json,"
        "certificate_json=excluded.certificate_json",
        (
            round_key,
            meter,
            state,
            tolerance,
            _json(certificate["corrections"]),
            _json(certificate["counterexamples"]),
            _json(certificate["uncovered_segments"]),
            _json(certificate["admissible_curves"]),
            _json(certificate),
        ),
    )
    return certificate


def get_certificate(db, round_id, meter=None):
    round_key = f"{meter}:{round_id}" if meter is not None else str(round_id)
    row = db.execute(
        "SELECT certificate_json FROM bench_certificate WHERE round_id=?",
        (round_key,),
    ).fetchone()
    return json.loads(row["certificate_json"]) if row else None


def query_display(db, round_id, display, meter=None):
    """Query one display: correction or concrete counterexample loads."""
    certificate = get_certificate(db, round_id, meter)
    if certificate is None:
        raise KeyError(f"no certificate for round {round_id}")
    for row in certificate["displays"]:
        if row["display"] == display:
            return row
    raise KeyError(f"display {display} outside certified range")


def query_curves(db, round_id, meter=None):
    """Full instrument curves still compatible with the round's evidence."""
    certificate = get_certificate(db, round_id, meter)
    if certificate is None:
        raise KeyError(f"no certificate for round {round_id}")
    return certificate["admissible_curves"]


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)
