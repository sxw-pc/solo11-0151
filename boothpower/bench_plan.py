"""Plan the single follow-up station slot.

Only one actual station slot may be consumed. The planner chooses, from the
loads visible at the stations, the action that best separates the response
lines that still matter for the target interval. It never assumes the most
favourable reading: every display the admissible curves can produce at the
chosen load becomes an executable outcome with the certificate that outcome
would support. When no action can fully witness the interval, the remaining
station-level evidence gap is returned.
"""

from collections import defaultdict

from .bench_certification import build_certificate
from .bench_curve import candidate_curves, curve_label, filter_curves
from .bench_intake import BENCH_STATION, FIELD_STATION


def _outcomes(case, curves, load, prior_observations, tolerance):
    groups = defaultdict(list)
    for curve in curves:
        groups[curve["responses"][load]].append(curve)
    result = []
    for display in sorted(groups):
        extended = [*prior_observations, (load, display)]
        certificate = build_certificate(case, extended, tolerance)
        result.append(
            {
                "display": display,
                "curve_count": len(groups[display]),
                "curves": [curve_label(curve) for curve in groups[display]],
                "certificate": certificate,
            }
        )
    return result


def _score(case, action, curves, prior_observations, tolerance, baseline_certified):
    outcomes = _outcomes(case, curves, action["load"], prior_observations, tolerance)
    if not outcomes:
        return {
            "outcomes": outcomes,
            "worst_certified": 0,
            "best_certified": 0,
            "expected_certified": 0,
            "expected_new_coverage": 0,
            "partitions": 0,
            "guarantees_saturated": False,
            "discriminating": False,
        }
    certifiable = [len(o["certificate"]["corrections"]) for o in outcomes]
    saturated_signed = [o["certificate"]["saturated_display"]["certifiable"] for o in outcomes]
    expected_certified = sum(certifiable) / len(certifiable)
    return {
        "outcomes": outcomes,
        "worst_certified": min(certifiable),
        "best_certified": max(certifiable),
        "expected_certified": expected_certified,
        "expected_new_coverage": expected_certified - baseline_certified,
        "partitions": len(outcomes),
        "guarantees_saturated": all(saturated_signed),
        "discriminating": len(outcomes) > 1,
    }


def plan_actions(case, prior_observations, bench_loads, field_steps, tolerance=0.5):
    """Evaluate every legal single action and rank them.

    ``field_steps`` are the load changes actually visible on the live circuit;
    bench loads are independently chosen standard loads. Ranking prefers
    actions that fully certify the interval, then the expected number of new
    displays the follow-up adds beyond the current certificate (averaged over
    every possible reading), then how many response groups the action splits,
    then the smaller load, then the bench station (bench excitation is
    independently controlled).
    """
    curves = filter_curves(candidate_curves(case), prior_observations)
    baseline = build_certificate(case, prior_observations, tolerance)
    baseline_certified = len(baseline["corrections"])
    plan = {
        "admissible_curves": [curve_label(curve) for curve in curves],
    }
    if not curves:
        plan.update(
            actions=[],
            chosen_action=None,
            evidence_gap="observations contradict every candidate response line",
        )
        return plan
    actions = [
        {"station": BENCH_STATION, "load": load} for load in bench_loads
    ] + [
        {"station": FIELD_STATION, "load": step} for step in field_steps
    ]
    evaluated = []
    for action in actions:
        detail = _score(case, action, curves, prior_observations, tolerance,
                        baseline_certified)
        evaluated.append({**action, **detail})
    evaluated.sort(
        key=lambda item: (
            not item["guarantees_saturated"],
            -item["expected_new_coverage"],
            -item["partitions"],
            item["load"],
            item["station"] != BENCH_STATION,
        )
    )
    chosen = evaluated[0] if evaluated else None
    plan = {
        "admissible_curves": [curve_label(curve) for curve in curves],
        "actions": evaluated,
    }
    if chosen is None:
        plan["chosen_action"] = None
        plan["evidence_gap"] = "no station action is available for this round"
        return plan
    plan["chosen_action"] = {
        "station": chosen["station"],
        "load": chosen["load"],
        "discriminating": chosen["discriminating"],
    }
    if chosen["guarantees_saturated"] and chosen["partitions"] == 1 and len(curves) == 1:
        plan["evidence_gap"] = None
    elif not chosen["discriminating"]:
        plan["evidence_gap"] = (
            f"every admissible response gives the same display at load {chosen['load']}; "
            "the slot would not cover the unverified high interval"
        )
    elif not chosen["guarantees_saturated"]:
        unresolved = _saturated_gap(chosen)
        plan["evidence_gap"] = (
            "no single action certifies the saturated display in every possible outcome; "
            + unresolved
        )
    else:
        plan["evidence_gap"] = None
    return plan


def _saturated_gap(chosen):
    options = sorted(
        {
            load
            for outcome in chosen["outcomes"]
            for load in outcome["certificate"]["saturated_display"]["possible_loads"]
        }
    )
    return f"saturated display true load remains among {options} in some outcomes"
