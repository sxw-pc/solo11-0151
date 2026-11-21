"""Local intake for reusable meter bench comparisons.

The historical single-point verdict stays exactly as before; the new
range-certification entry points additionally fix one verification round,
ingest bench/field originals and issue certificates scoped to that round and
the meter's current seal.
"""

from . import bench_certification, bench_intake, bench_plan
from .bench_result import classify
from .source import record


def import_data(db, data):
    """Original bench-import behaviour: record each stake and classify it."""
    with db:
        results = []
        for sample in data["comparisons"]:
            record(db, sample["id"], "bench_comparison", sample)
            results.append(classify(db, sample))
    return results


def _starting_observations(case):
    return [tuple(pair) for pair in case["starting_pairs"]]


STATIONS = ("bench", "field")
MAX_CANDIDATE_RESPONSES = 10
MAX_FIELD_SETS = 2


def _validate_payload(data):
    """Enforce the bounded single-meter, two-station verification scope."""
    scope = data.get("station_scope", list(STATIONS))
    if any(station not in STATIONS for station in scope):
        raise ValueError(f"unknown station in scope {scope}; allowed: {STATIONS}")
    if len(scope) > len(STATIONS):
        raise ValueError("one meter may only use the bench and field stations")
    case = data["response_case"]
    curve_count = len(case["candidate_slopes"]) * len(case["candidate_offsets"])
    if curve_count > MAX_CANDIDATE_RESPONSES:
        raise ValueError(
            f"{curve_count} candidate responses exceed the limit of {MAX_CANDIDATE_RESPONSES}"
        )
    field_sets = data.get("field_sets", [])
    if len(field_sets) > MAX_FIELD_SETS:
        raise ValueError(
            f"{len(field_sets)} field originals exceed the limit of {MAX_FIELD_SETS}"
        )
    if field_sets and "field" not in scope:
        raise ValueError("field originals require the field station in scope")
    if data.get("bench_observations") and "bench" not in scope:
        raise ValueError("bench originals require the bench station in scope")
    low, high = case["actual_range"]
    if not (0 <= low <= high <= case["max_display"]):
        raise ValueError("actual load range must sit inside the display range")


def certify_data(db, data):
    """Run a full bench re-verification round from one certification payload."""
    _validate_payload(data)
    case = data["response_case"]
    tolerance = data.get("tolerance", 0.5)
    meter = data["meter"]
    bench_intake.open_round(db, meter, data.get("round_id"))
    round_id = bench_intake.current_round(db, meter)
    with db:
        for i, (load, display) in enumerate(_starting_observations(case)):
            bench_intake.add_bench_observation(
                db, meter, f"{data.get('evidence_prefix', 'BENCH')}-start-{i}",
                load, display, round_id,
            )
        for observation in data.get("bench_observations", []):
            bench_intake.add_bench_observation(
                db, meter, observation["event_id"],
                observation["true_load"], observation["display"], round_id,
            )
        for field_set in data.get("field_sets", []):
            field_set = dict(field_set)
            field_set["round_id"] = round_id
            bench_intake.add_field_set(db, meter, field_set)
        certificate = bench_certification.certify_round(
            db, round_id, case, tolerance, meter
        )
    plan = bench_plan.plan_actions(
        case,
        [(row["true_load"], row["display"])
         for row in bench_intake.round_observations(db, round_id)],
        data.get("bench_candidates", []),
        data.get("field_candidates", []),
        tolerance,
    )
    return {
        "meter": meter,
        "round_id": round_id,
        "station_scope": data.get("station_scope", ["bench", "field"]),
        "certificate": certificate,
        "plan": plan,
    }


def plan_data(db, data):
    """Plan the one follow-up slot from currently visible station candidates."""
    _validate_payload(data)
    case = data["response_case"]
    tolerance = data.get("tolerance", 0.5)
    observations = _starting_observations(case)
    meter = data["meter"]
    if "round_id" in data:
        observations = [
            (row["true_load"], row["display"])
            for row in bench_intake.round_observations(db, data["round_id"])
        ] or observations
    return {
        "meter": meter,
        "plan": bench_plan.plan_actions(
            case,
            observations,
            data.get("bench_candidates", []),
            data.get("field_candidates", []),
            tolerance,
        ),
    }
