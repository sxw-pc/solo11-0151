"""Acceptance tests for bench range certification.

Runnable with ``python -B -m pytest -q`` and also with the stdlib unittest
runner (no third-party dependency). All fixtures are synthetic offline data.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from boothpower import bench_capture, bench_certification, bench_intake, bench_plan
from boothpower.bench_curve import candidate_curves, filter_curves
from boothpower.store import connect

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class BenchCertificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(":memory:")
        self.initial = fixture("bench_certify_initial.json")
        self.case = self.initial["response_case"]

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _certify(self, name):
        return bench_capture.certify_data(self.db, fixture(name))

    # 1. Single-point pass must not certify the saturated end.
    def test_single_point_certificate_holds_saturated_display(self):
        out = bench_capture.certify_data(self.db, self.initial)
        cert = out["certificate"]
        self.assertEqual(cert["state"], "partial")
        # Reliable low-range corrections that hold on all three response lines.
        self.assertEqual(cert["corrections"], {0: 0, 2: 1, 3: 2, 4: 3, 8: 7})
        saturated = cert["saturated_display"]
        self.assertFalse(saturated["certifiable"])
        self.assertEqual(saturated["possible_loads"], [5, 6, 7, 8])
        self.assertEqual(9, cert["counterexamples"][-1]["display"])
        # No interpolation: display 9 keeps its full unresolved load band.
        self.assertEqual(cert["uncovered_segments"],
                         [{"from": 1, "to": 1}, {"from": 5, "to": 7}, {"from": 9, "to": 9}])

    def test_three_response_lines_survive_starting_stake(self):
        out = bench_capture.certify_data(self.db, self.initial)
        labels = out["certificate"]["admissible_curves"]
        self.assertEqual(
            sorted((c["slope"], c["offset"]) for c in labels),
            [(1, 1), (1.5, 0), (2, -1)],
        )
        # At most ten candidate responses overall, exactly three fit the stake.
        self.assertEqual(len(candidate_curves(self.case)), 9)
        self.assertEqual(out["certificate"]["admissible_curve_count"], 3)

    def test_display_correction_queries_and_counterexamples(self):
        out = bench_capture.certify_data(self.db, self.initial)
        round_id = out["round_id"]
        self.assertEqual(
            bench_certification.query_display(self.db, round_id, 3, "SPARE-1"),
            {
                "display": 3,
                "possible_loads": [2],
                "explaining_curves": [
                    {"slope": 1, "offset": 1},
                    {"slope": 1.5, "offset": 0},
                    {"slope": 2, "offset": -1},
                ],
                "correction": 2,
                "certifiable": True,
            },
        )
        held = bench_certification.query_display(self.db, round_id, 9, "SPARE-1")
        self.assertFalse(held["certifiable"])
        self.assertIsNone(held["correction"])
        self.assertEqual(held["possible_loads"], [5, 6, 7, 8])
        curves = bench_certification.query_curves(self.db, round_id, "SPARE-1")
        self.assertEqual(len(curves), 3)

    def test_endpoint_only_saturation_match_is_not_dropped(self):
        # Curves that only reach display 9 at load 8 (the clipped endpoint)
        # must still explain an observed (8, 9) stake.
        curves = filter_curves(candidate_curves(self.case), [(2, 3), (8, 9)])
        self.assertEqual(len(curves), 3)
        labels = sorted((c["slope"], c["offset"]) for c in curves)
        self.assertEqual(labels, [(1, 1), (1.5, 0), (2, -1)])

    # 2. Both legal sampling routes work, each on its own terms.
    def test_bench_route_load5_display6_pins_slope_one_offset_one(self):
        out = self._certify("bench_certify_bench_full.json")
        cert = out["certificate"]
        self.assertEqual(cert["state"], "signed")
        self.assertEqual(cert["admissible_curves"], [{"slope": 1, "offset": 1}])
        self.assertEqual(cert["corrections"][9], 8)
        for display, load in cert["corrections"].items():
            self.assertLessEqual(abs(display - 1 - load), 0.5)

    def test_field_route_step4_display6_pins_slope_one_half_zero(self):
        out = self._certify("bench_certify_field_partial.json")
        cert = out["certificate"]
        self.assertEqual(cert["state"], "partial")
        self.assertEqual(cert["admissible_curves"], [{"slope": 1.5, "offset": 0}])
        # Low end is reliable, high end remains reserved.
        self.assertEqual(cert["corrections"][3], 2)
        self.assertEqual(cert["corrections"][6], 4)
        saturated = cert["saturated_display"]
        self.assertFalse(saturated["certifiable"])
        self.assertEqual(saturated["possible_loads"], [6, 7, 8])

    def test_two_routes_are_not_interchangeable(self):
        bench_db = connect(":memory:")
        field_db = connect(":memory:")
        try:
            bench_out = bench_capture.certify_data(
                bench_db, fixture("bench_certify_bench_full.json"))
            field_out = bench_capture.certify_data(
                field_db, fixture("bench_certify_field_partial.json"))
            # Same display value 6, different corrected load, different stations.
            self.assertEqual(bench_out["certificate"]["corrections"][6], 5)
            self.assertEqual(field_out["certificate"]["corrections"][6], 4)
            bench_rows = bench_intake.round_observations(
                bench_db, bench_out["round_id"])
            field_rows = bench_intake.round_observations(
                field_db, field_out["round_id"], "field")
            self.assertTrue(all(row["station"] == "bench" for row in bench_rows))
            self.assertTrue(all(row["station"] == "field" for row in field_rows))
            # The field route binds two raw sequences and one shared excitation.
            bound = field_db.execute(
                "SELECT COUNT(*) AS n FROM field_reading WHERE excitation_id=?",
                ("EXC-STEP-2-4",),
            ).fetchone()
            self.assertEqual(bound["n"], 2)
            excitation = field_db.execute(
                "SELECT * FROM field_excitation WHERE id=?", ("EXC-STEP-2-4",)
            ).fetchone()
            self.assertEqual((excitation["load_before"], excitation["load_after"]), (2, 4))
        finally:
            bench_db.close()
            field_db.close()

    def test_field_set_rejects_unbound_or_mismatched_sequences(self):
        bench_intake.open_round(self.db, "SPARE-1", 1)
        good = fixture("bench_certify_field_partial.json")["field_sets"][0]
        # Different circuits: the two meters do not see the same loop.
        bad_circuit = json.loads(json.dumps(good))
        bad_circuit["readings"][1]["circuit"] = "C2"
        with self.assertRaises(ValueError):
            bench_intake.add_field_set(self.db, "SPARE-1", bad_circuit)
        # Different excitation ids: sequences are not bound to one stimulus.
        bad_excitation = json.loads(json.dumps(good))
        bad_excitation["readings"][1]["excitation_id"] = "EXC-OTHER"
        with self.assertRaises(ValueError):
            bench_intake.add_field_set(self.db, "SPARE-1", bad_excitation)
        # One meter only is not a joint reading.
        bad_single = json.loads(json.dumps(good))
        bad_single["readings"].pop()
        with self.assertRaises(ValueError):
            bench_intake.add_field_set(self.db, "SPARE-1", bad_single)
        # Reference bias must never act as truth: no load change, no anchor.
        bad_step = json.loads(json.dumps(good))
        bad_step["excitation"]["load_after"] = 2
        with self.assertRaises(ValueError):
            bench_intake.add_field_set(self.db, "SPARE-1", bad_step)

    # 3. Low end signed, high end keeps a counterexample.
    def test_low_end_signed_but_high_end_keeps_counterexample(self):
        out = self._certify("bench_certify_field_partial.json")
        cert = out["certificate"]
        self.assertIn(3, cert["corrections"])
        high = next(item for item in cert["counterexamples"] if item["display"] == 9)
        self.assertEqual(high["possible_loads"], [6, 7, 8])

    # 4. Re-sealed meter: old originals cannot support the new round.
    def test_reseal_voids_previous_round_originals(self):
        first = bench_capture.certify_data(self.db, self.initial)
        self.assertEqual(first["round_id"], 1)
        bench_intake.open_round(self.db, "SPARE-1")  # technician breaks the seal
        self.assertEqual(bench_intake.current_round(self.db, "SPARE-1"), 2)
        with self.assertRaises(ValueError):
            bench_intake.add_bench_observation(
                self.db, "SPARE-1", "LATE-BENCH", 5, 6, round_id=1
            )
        # The round-1 certificate is retained as history but no round-2
        # certificate may lean on round-1 evidence.
        history = bench_certification.get_certificate(self.db, 1, "SPARE-1")
        self.assertIsNotNone(history)
        self.assertIsNone(bench_certification.get_certificate(self.db, 2, "SPARE-1"))

    # 5. Existing entry point and raw replay stay identical.
    def test_legacy_bench_import_output_unchanged(self):
        data = fixture("bench_comparisons.json")
        output = bench_capture.import_data(self.db, data)
        self.assertEqual([row["state"] for row in output], ["calibrated", "held"])
        # Replay is idempotent and keeps the same verdicts.
        again = bench_capture.import_data(self.db, data)
        self.assertEqual([row["state"] for row in again], ["calibrated", "held"])

    def test_cli_bench_import_raw_replay_matches(self):
        path = str(Path(self.tmp.name) / "cli.sqlite")
        run = subprocess.run(
            [sys.executable, "-m", "boothpower", "bench-import",
             "--db", path, "--input", str(FIXTURES / "bench_comparisons.json")],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
        self.assertEqual(
            [row["state"] for row in json.loads(run.stdout)], ["calibrated", "held"]
        )
        run2 = subprocess.run(
            [sys.executable, "-m", "boothpower", "bench-certify",
             "--db", path, "--input", str(FIXTURES / "bench_certify_initial.json")],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
        payload = json.loads(run2.stdout)
        self.assertEqual(payload["certificate"]["state"], "partial")
        self.assertEqual(payload["round_id"], 1)

    # Planner: one slot, every possible outcome enumerated, no optimistic read.
    def test_planner_picks_best_visible_action_and_lists_all_outcomes(self):
        out = bench_capture.certify_data(self.db, self.initial)
        plan = out["plan"]
        self.assertEqual(plan["chosen_action"],
                         {"station": "bench", "load": 5, "discriminating": True})
        action = next(a for a in plan["actions"]
                      if a["station"] == "bench" and a["load"] == 5)
        displays = sorted(o["display"] for o in action["outcomes"])
        self.assertEqual(displays, [6, 7, 9])
        labels = {
            o["display"]: sorted((c["slope"], c["offset"]) for c in o["curves"])
            for o in action["outcomes"]
        }
        self.assertEqual(labels[6], [(1, 1)])
        self.assertEqual(labels[7], [(1.5, 0)])
        self.assertEqual(labels[9], [(2, -1)])
        # The promised full certificate only follows the display-6 outcome.
        outcome6 = next(o for o in action["outcomes"] if o["display"] == 6)
        self.assertEqual(outcome6["certificate"]["corrections"][9], 8)
        self.assertIsNotNone(plan["evidence_gap"])
        self.assertFalse(action["guarantees_saturated"])

    def test_planner_field_small_step_does_not_cover_high_end(self):
        out = self._certify("bench_certify_field_saturated.json")
        plan = out["plan"]
        self.assertEqual(plan["chosen_action"],
                         {"station": "field", "load": 8, "discriminating": False})
        action = plan["actions"][0]
        self.assertEqual([o["display"] for o in action["outcomes"]], [9])
        self.assertIsNotNone(plan["evidence_gap"])
        self.assertIn("same display", plan["evidence_gap"])

    def test_field_partial_plan_keeps_high_end_gap_after_single_curve(self):
        out = self._certify("bench_certify_field_partial.json")
        plan = out["plan"]
        self.assertIsNotNone(plan["evidence_gap"])
        self.assertFalse(plan["actions"][0]["guarantees_saturated"])

    def test_alternative_candidate_sets_can_full_certify_either_route(self):
        base = fixture("bench_certify_initial.json")
        # Bench route with a different visible standard load: load 4, and the
        # display-5 response still isolates (1, 1) and signs the saturated end.
        bench = json.loads(json.dumps(base))
        bench["bench_candidates"] = [4]
        bench["field_candidates"] = []
        bench_plan_out = bench_capture.plan_data(self.db, bench)["plan"]
        action = bench_plan_out["actions"][0]
        self.assertEqual((action["station"], action["load"]), ("bench", 4))
        self.assertTrue(
            any(o["certificate"]["saturated_display"]["certifiable"]
                for o in action["outcomes"])
        )
        # Field route with load step 4: the display-5 joint outcome isolates
        # (1, 1) and fully certifies under the same starting stake.
        field = json.loads(json.dumps(base))
        field["bench_candidates"] = []
        field["field_candidates"] = [4]
        field_plan_out = bench_capture.plan_data(self.db, field)["plan"]
        field_action = field_plan_out["actions"][0]
        self.assertEqual((field_action["station"], field_action["load"]),
                         ("field", 4))
        self.assertTrue(
            any(o["certificate"]["saturated_display"]["certifiable"]
                for o in field_action["outcomes"])
        )

    def test_tolerance_blocks_correction_when_explanations_differ(self):
        stricter = json.loads(json.dumps(self.initial))
        stricter["tolerance"] = 0
        cert = bench_capture.certify_data(self.db, stricter)["certificate"]
        # Display 3 still unique (load 2); ambiguous displays stay unsigned.
        self.assertEqual(cert["corrections"][3], 2)
        self.assertNotIn(9, cert["corrections"])

    def test_contradictory_originals_leave_no_admissible_curve(self):
        data = json.loads(json.dumps(self.initial))
        # Same load 2 with a second display cannot match any single fixed curve.
        data["bench_observations"] = [
            {"event_id": "CAL-CONTRA", "true_load": 2, "display": 4}
        ]
        out = bench_capture.certify_data(self.db, data)
        cert = out["certificate"]
        self.assertEqual(cert["state"], "contradictory")
        self.assertEqual(cert["admissible_curves"], [])
        self.assertEqual(cert["corrections"], {})
        plan = out["plan"]
        self.assertIsNone(plan["chosen_action"])
        self.assertIn("contradict", plan["evidence_gap"])

    def test_scope_is_one_meter_two_stations_at_most_ten_responses(self):
        data = json.loads(json.dumps(self.initial))
        data["station_scope"] = ["bench", "field", "lab"]
        with self.assertRaises(ValueError):
            bench_capture.certify_data(self.db, data)
        too_many = json.loads(json.dumps(self.initial))
        too_many["response_case"]["candidate_slopes"] = [1, 1.25, 1.5, 1.75]
        with self.assertRaises(ValueError):
            bench_capture.certify_data(self.db, too_many)
        three_sets = json.loads(json.dumps(self.initial))
        base_set = fixture("bench_certify_field_partial.json")["field_sets"][0]
        three_sets["station_scope"] = ["field"]
        three_sets["field_sets"] = []
        for index in range(3):
            field_set = json.loads(json.dumps(base_set))
            field_set["excitation"]["id"] = f"EXC-{index}"
            for reading in field_set["readings"]:
                reading["excitation_id"] = f"EXC-{index}"
                reading["event_id"] = f"{reading['event_id']}-{index}"
            three_sets["field_sets"].append(field_set)
        with self.assertRaises(ValueError):
            bench_capture.certify_data(self.db, three_sets)
        # Field originals without the field station in scope are rejected.
        scoped = json.loads(json.dumps(self.initial))
        scoped["station_scope"] = ["bench"]
        scoped["field_sets"] = [base_set]
        with self.assertRaises(ValueError):
            bench_capture.certify_data(self.db, scoped)

    def test_two_field_originals_are_accepted_and_bound(self):
        data = json.loads(json.dumps(self.initial))
        base_set = fixture("bench_certify_field_partial.json")["field_sets"][0]
        data["station_scope"] = ["field"]
        data["field_sets"] = []
        for index in range(2):
            field_set = json.loads(json.dumps(base_set))
            field_set["excitation"]["id"] = f"EXC-STEP-{index}"
            for reading in field_set["readings"]:
                reading["excitation_id"] = f"EXC-STEP-{index}"
                reading["event_id"] = f"{reading['event_id']}-{index}"
            data["field_sets"].append(field_set)
        out = bench_capture.certify_data(self.db, data)
        bound = self.db.execute(
            "SELECT COUNT(DISTINCT excitation_id) AS n FROM field_reading"
        ).fetchone()
        self.assertEqual(bound["n"], 2)
        self.assertEqual(out["certificate"]["state"], "partial")

    def test_pure_certificate_does_not_interpolate_saturated_end(self):
        cert = bench_certification.build_certificate(self.case, [(2, 3)])
        # The saturated display keeps the full 5..8 band instead of a guessed value.
        self.assertEqual(cert["saturated_display"]["possible_loads"], [5, 6, 7, 8])
        self.assertFalse(cert["saturated_display"]["certifiable"])


if __name__ == "__main__":
    unittest.main()
