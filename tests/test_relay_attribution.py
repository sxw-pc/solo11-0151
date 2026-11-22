"""Original-document attribution for synthetic dual-endpoint relay trips.

These tests pin the offline evidence rules: a coarse "both dropped" summary
never signs a ticket by itself; meter offsets and the 2-4 tick propagation
band keep both histories open; only a complete common-edge strip or a
countersigned drive-contact custody chain can name the originator;
contradictory originals stop the case instead of picking a side; and at most
one new evidence-gathering action is ever proposed.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from boothpower import relay_capture, relay_owner
from boothpower.store import connect

ROOT = Path(__file__).resolve().parents[1]
ROUND = "round-0"
BOUNDS = [-2, 2]


def base_clocks():
    return {node: {"reading": 10, "offset_bounds": list(BOUNDS)} for node in ("U", "R", "V")}


def base_payload(**overrides):
    data = {
        "observations": [
            {"id": "note-1", "case": "R-1", "root_hint": None, "dropped": ["U", "V"]},
        ],
        "meter_readings": [
            {"event_id": f"read-{node}", "case": "R-1", "node": node,
             "contact": f"{node}-edge", "reading": 10}
            for node in ("U", "R", "V")
        ],
        "clocks": base_clocks(),
        "permitted_ordering": [
            {"root": "U", "edges": [9, 11]},
            {"root": "V", "edges": [11, 9]},
        ],
        "local_evidence_options": ["common-edge-recorder", "drive-contact-custody"],
    }
    data.update(overrides)
    return data


def wiring(version, signers):
    return {
        "event_id": f"wiring-{version}",
        "case": "R-1",
        "version": version,
        "signers": signers,
        "lines": [
            {"node": "U", "contact": "U-c1", "role": "local_send"},
            {"node": "U", "contact": "U-c2", "role": "device_echo"},
            {"node": "V", "contact": "V-c1", "role": "local_send"},
            {"node": "V", "contact": "V-c2", "role": "device_echo"},
        ],
    }


def strip_u(version="A"):
    return {
        "event_id": "strip-u",
        "case": "R-1",
        "version": version,
        "signers": ["u-tech", "v-tech"],
        "window_complete": True,
        "cleared_after_window": False,
        "edges": [
            {"node": "U", "contact": "U-c1", "scale_tick": 9},
            {"node": "R", "contact": "R-c1", "scale_tick": 12},
            {"node": "V", "contact": "V-c2", "scale_tick": 15},
        ],
    }


def chain(event_id, send, send_contact, receive, receive_contact,
          signers=("u-tech", "v-tech"), mirror=False, damaged=False, drive="drive-1"):
    return {
        "event_id": event_id,
        "case": "R-1",
        "signers": list(signers),
        "links": [{
            "drive_id": drive,
            "send_node": send,
            "send_contact": send_contact,
            "receive_node": receive,
            "receive_contact": receive_contact,
            "mirror": mirror,
            "send_contact_damaged": damaged,
        }],
    }


class RelayAttributionTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")

    def tearDown(self):
        self.db.close()

    def verdict(self, case="R-1", round_id="round-0"):
        return relay_owner.reconcile(self.db, case, round_id)[0]

    def import_case(self, **overrides):
        return relay_capture.import_data(self.db, base_payload(**overrides))["cases"][0]

    # -- coarse summaries and meter feasibility ---------------------------

    def test_dual_drop_summary_with_signed_hint_stays_unassigned(self):
        payload = base_payload()
        payload["observations"].append(
            {"id": "note-2", "case": "R-1", "root_hint": "U", "dropped": ["U", "V"]}
        )
        case = relay_capture.import_data(self.db, payload)["cases"][0]
        self.assertEqual(case["state"], "pending_verification")
        self.assertIsNone(case["owner"])
        self.assertEqual(case["work_orders"], [])
        stored = {
            json.loads(row["raw"])["id"]: json.loads(row["raw"]).get("root_hint")
            for row in self.db.execute(
                "SELECT raw FROM evidence WHERE kind='relay_observation'"
            )
        }
        self.assertEqual(stored, {"note-1": None, "note-2": "U"})

    def test_both_histories_survive_when_each_meter_keeps_its_own_offset(self):
        # U local 9 / V local 11 versus U 11 / V 9 are both expressible on the
        # common scale inside [-2, 2] offsets and the 2-4 tick propagation band.
        case = self.import_case()
        self.assertEqual(case["compatible_roots"], ["U", "V"])
        self.assertEqual(case["state"], "pending_verification")

    def test_one_offset_per_meter_per_round_can_shut_a_direction(self):
        # Two relay readings pin its offset to 0.  U=8 / R=11 / V=13 on the
        # common scale fit U->R->U gaps 3 and 2; the reverse direction would
        # need negative 2-4 gaps, which no [-2, 2] offset choice can produce.
        payload = base_payload()
        payload["meter_readings"] = [
            {"event_id": "read-U", "case": "R-1", "node": "U", "contact": "U-edge", "reading": 8},
            {"event_id": "read-R-a", "case": "R-1", "node": "R", "contact": "R-a", "reading": 11},
            {"event_id": "read-R-b", "case": "R-1", "node": "R", "contact": "R-b", "reading": 11},
            {"event_id": "read-V", "case": "R-1", "node": "V", "contact": "V-edge", "reading": 13},
        ]
        case = relay_capture.import_data(self.db, payload)["cases"][0]
        self.assertEqual(case["compatible_roots"], ["U"])

    def test_blank_reading_after_reset_cannot_veto_an_old_contact(self):
        payload = base_payload()
        payload["meter_readings"].append(
            {"event_id": "read-V-blank", "case": "R-1", "node": "V",
             "contact": "V-edge", "missing": True, "reading": None}
        )
        case = relay_capture.import_data(self.db, payload)["cases"][0]
        self.assertEqual(case["compatible_roots"], ["U", "V"])

    def test_arrival_order_of_files_is_not_timing_evidence(self):
        payload = base_payload()
        first = relay_capture.import_data(self.db, payload)["cases"][0]
        other_db = connect(":memory:")
        try:
            reversed_payload = json.loads(json.dumps(payload))
            reversed_payload["meter_readings"] = list(reversed(reversed_payload["meter_readings"]))
            second = relay_capture.import_data(other_db, reversed_payload)["cases"][0]
            self.assertEqual(first["compatible_roots"], second["compatible_roots"])
            self.assertEqual(first["state"], second["state"])
        finally:
            other_db.close()

    # -- two wiring bitmap versions ---------------------------------------

    def test_unsigned_wiring_versions_block_contact_role_attribution(self):
        payload = base_payload()
        payload["wiring_versions"] = [wiring("A", ["u-tech"]), wiring("B", ["v-tech"])]
        case = relay_capture.import_data(self.db, payload)["cases"][0]
        self.assertEqual(case["state"], "pending_verification")
        self.assertEqual(case["action"]["procedure"], "wiring-signoff")
        self.assertEqual({c["node"] for c in case["action"]["target_contacts"]}, {"U", "V"})
        self.assertEqual(case["work_orders"], [])

    def test_two_fully_signed_but_contradictory_wiring_versions_conflict(self):
        version_b = wiring("B", ["u-tech", "v-tech"])
        for line in version_b["lines"]:
            line["role"] = "device_echo" if line["role"] == "local_send" else "local_send"
        payload = base_payload()
        payload["wiring_versions"] = [wiring("A", ["u-tech", "v-tech"]), version_b]
        case = relay_capture.import_data(self.db, payload)["cases"][0]
        self.assertEqual(case["state"], "evidence_conflict")
        self.assertEqual(case["conflicts"][0]["procedure"], "wiring-signoff")
        self.assertIsNone(case["owner"])

    def test_signed_wiring_allows_the_complete_strip_to_dispatch_u(self):
        payload = base_payload()
        payload["wiring_versions"] = [wiring("A", ["u-tech", "v-tech"])]
        payload["common_edge_records"] = [strip_u()]
        case = relay_capture.import_data(self.db, payload)["cases"][0]
        self.assertEqual(case["state"], "assigned")
        self.assertEqual(case["owner"], "U")
        order = case["work_orders"][0]
        self.assertEqual(order["owner"], "U")
        ref = order["evidence_refs"][0]
        self.assertEqual(ref["event_id"], "strip-u")
        self.assertEqual(ref["round_id"], ROUND)
        self.assertEqual(ref["contacts"][0]["contact"], "U-c1")
        self.assertTrue(any("U local-send contact U-c1" in line for line in order["basis"]))

    def test_strip_without_two_side_signature_is_not_an_original(self):
        payload = base_payload()
        payload["wiring_versions"] = [wiring("A", ["u-tech", "v-tech"])]
        strip = strip_u()
        strip["signers"] = ["u-tech"]
        payload["common_edge_records"] = [strip]
        case = relay_capture.import_data(self.db, payload)["cases"][0]
        self.assertEqual(case["state"], "pending_verification")
        self.assertIsNone(case["owner"])

    def test_spare_strip_after_cleared_contacts_cannot_attribute_from_dark_lamps(self):
        payload = base_payload()
        payload["wiring_versions"] = [wiring("A", ["u-tech", "v-tech"])]
        payload["common_edge_records"] = [{
            "event_id": "strip-late",
            "case": "R-1",
            "version": "A",
            "signers": ["u-tech", "v-tech"],
            "window_complete": True,
            "cleared_after_window": True,
            "edges": [
                {"node": "U", "contact": "U-c1", "scale_tick": 20},
                {"node": "V", "contact": "V-c2", "scale_tick": 23},
            ],
        }]
        case = relay_capture.import_data(self.db, payload)["cases"][0]
        self.assertIsNone(case["owner"])
        self.assertIn(case["state"], ("pending_verification", "awaiting_source"))
        finding = relay_owner.evaluate_common_recorder(
            self.db, relay_owner.feasible_roots(self.db, "R-1")[1]
        )[0]
        self.assertEqual(finding["state"], "unusable")
        self.assertTrue(any("cleared" in reason for reason in finding["reasons"]))

    def test_incomplete_window_strip_is_unusable_not_conflicting(self):
        payload = base_payload()
        payload["wiring_versions"] = [wiring("A", ["u-tech", "v-tech"])]
        payload["common_edge_records"] = [{
            "event_id": "strip-gap",
            "case": "R-1",
            "version": "A",
            "signers": ["u-tech", "v-tech"],
            "window_complete": False,
            "cleared_after_window": False,
            "edges": [{"node": "U", "contact": "U-c1", "scale_tick": 9}],
        }]
        case = relay_capture.import_data(self.db, payload)["cases"][0]
        self.assertEqual(case["conflicts"], [])
        self.assertIsNone(case["owner"])

    def test_strip_violating_propagation_band_is_a_named_conflict(self):
        payload = base_payload()
        payload["wiring_versions"] = [wiring("A", ["u-tech", "v-tech"])]
        payload["common_edge_records"] = [{
            "event_id": "strip-bad",
            "case": "R-1",
            "version": "A",
            "signers": ["u-tech", "v-tech"],
            "window_complete": True,
            "cleared_after_window": False,
            "edges": [
                {"node": "U", "contact": "U-c1", "scale_tick": 9},
                {"node": "R", "contact": "R-c1", "scale_tick": 10},
                {"node": "V", "contact": "V-c2", "scale_tick": 20},
            ],
        }]
        case = relay_capture.import_data(self.db, payload)["cases"][0]
        self.assertEqual(case["state"], "evidence_conflict")
        conflict = case["conflicts"][0]
        self.assertEqual(conflict["event_id"], "strip-bad")
        self.assertTrue(any("2-4 tick" in reason for reason in conflict["reasons"]))
        self.assertEqual(case["work_orders"], [])

    def test_strip_first_edge_on_echo_contact_is_conflict_not_a_dispatch(self):
        payload = base_payload()
        payload["wiring_versions"] = [wiring("A", ["u-tech", "v-tech"])]
        payload["common_edge_records"] = [{
            "event_id": "strip-echo",
            "case": "R-1",
            "version": "A",
            "signers": ["u-tech", "v-tech"],
            "window_complete": True,
            "cleared_after_window": False,
            "edges": [
                {"node": "V", "contact": "V-c2", "scale_tick": 9},
                {"node": "R", "contact": "R-c1", "scale_tick": 12},
                {"node": "U", "contact": "U-c1", "scale_tick": 15},
            ],
        }]
        case = relay_capture.import_data(self.db, payload)["cases"][0]
        self.assertEqual(case["state"], "evidence_conflict")
        self.assertTrue(any("local-send" in reason
                            for reason in case["conflicts"][0]["reasons"]))

    # -- drive-contact custody chains -------------------------------------

    def test_custody_chain_proves_v_without_trusting_either_clock(self):
        payload = base_payload()
        payload["drive_contact_chains"] = [chain("chain-v", "V", "V-latch-1", "R", "R-in-1")]
        case = relay_capture.import_data(self.db, payload)["cases"][0]
        self.assertEqual(case["state"], "assigned")
        self.assertEqual(case["owner"], "V")
        order = case["work_orders"][0]
        ref = order["evidence_refs"][0]
        self.assertEqual(ref["procedure"], "drive-contact-custody")
        self.assertEqual(ref["contacts"][0]["send_contact"], "V-latch-1")
        self.assertTrue(any("without comparing either clock" in line for line in order["basis"]))

    def test_mirrored_shared_drive_is_not_a_second_origination(self):
        payload = base_payload()
        payload["drive_contact_chains"] = [
            chain("chain-v", "V", "V-latch-1", "R", "R-in-1"),
            chain("chain-mirror", "U", "U-echo-1", "R", "R-in-1", mirror=True),
        ]
        case = relay_capture.import_data(self.db, payload)["cases"][0]
        self.assertEqual(case["state"], "assigned")
        self.assertEqual(case["owner"], "V")
        findings = relay_owner.evaluate_drive_chain(
            self.db, relay_owner.feasible_roots(self.db, "R-1")[1]
        )
        self.assertTrue(any("mirrored" in reason for item in findings for reason in item["reasons"]))

    def test_two_first_sends_in_sealed_chains_are_a_conflict(self):
        payload = base_payload()
        payload["drive_contact_chains"] = [
            chain("chain-v", "V", "V-latch-1", "R", "R-in-1", drive="drive-1"),
            chain("chain-u", "U", "U-latch-1", "R", "R-in-2", drive="drive-2"),
        ]
        case = relay_capture.import_data(self.db, payload)["cases"][0]
        self.assertEqual(case["state"], "evidence_conflict")
        self.assertTrue(any(
            "cannot belong to one physical history" in reason
            for c in case["conflicts"] for reason in c["reasons"]
        ))
        self.assertEqual(
            {c["procedure"] for c in case["conflicts"]}, {"cross-procedure"}
        )

    def test_damaged_send_contact_keeps_the_chain_from_attributing(self):
        payload = base_payload()
        payload["drive_contact_chains"] = [
            chain("chain-broken", "U", "U-latch-1", "R", "R-in-1", damaged=True)
        ]
        case = relay_capture.import_data(self.db, payload)["cases"][0]
        self.assertIsNone(case["owner"])
        self.assertEqual(case["state"], "pending_verification")

    def test_custody_chain_may_prove_one_circuit_segment_only(self):
        # The custody chain need not time the full path: a sealed V->R
        # first-send/receive pair already names V without clock comparison.
        payload = base_payload()
        payload["drive_contact_chains"] = [
            chain("chain-segment", "V", "V-latch-2", "R", "R-in-7")
        ]
        case = relay_capture.import_data(self.db, payload)["cases"][0]
        self.assertEqual(case["owner"], "V")
        contact = case["work_orders"][0]["evidence_refs"][0]["contacts"][0]
        self.assertEqual((contact["send_node"], contact["receive_node"]), ("V", "R"))

    def test_rounds_are_kept_distinct_under_one_case(self):
        payload = base_payload()
        payload["round_id"] = "round-1"
        r1_chain = chain("chain-r1", "U", "U-latch", "R", "R-in")
        r1_chain["round_id"] = "round-1"
        payload["drive_contact_chains"] = [r1_chain]
        first = relay_capture.import_data(self.db, payload)["cases"][0]
        self.assertEqual(first["round_id"], "round-1")
        self.assertEqual(first["owner"], "U")
        # A later round starts unassigned even though the older round is settled.
        second = relay_capture.import_data(self.db, base_payload())["cases"][0]
        self.assertEqual(second["round_id"], "round-0")
        self.assertIsNone(second["owner"])
        self.assertEqual(second["state"], "pending_verification")

    def test_custody_chain_works_even_when_common_strip_route_is_unavailable(self):
        payload = base_payload()
        payload["local_evidence_options"] = ["drive-contact-custody"]
        payload["drive_contact_chains"] = [chain("chain-u", "U", "U-latch-7", "V", "V-in-2")]
        case = relay_capture.import_data(self.db, payload)["cases"][0]
        self.assertEqual(case["state"], "assigned")
        self.assertEqual(case["owner"], "U")

    def test_cross_procedure_originals_disagreeing_stop_at_evidence_conflict(self):
        payload = base_payload()
        payload["wiring_versions"] = [wiring("A", ["u-tech", "v-tech"])]
        payload["common_edge_records"] = [strip_u()]
        payload["drive_contact_chains"] = [chain("chain-v", "V", "V-latch-1", "R", "R-in-1")]
        case = relay_capture.import_data(self.db, payload)["cases"][0]
        self.assertEqual(case["state"], "evidence_conflict")
        self.assertIn("cross-procedure", {c["procedure"] for c in case["conflicts"]})
        self.assertEqual(case["work_orders"], [])

    # -- synthetic reset-gap control --------------------------------------

    def test_same_summary_is_dispatched_by_evidence_not_by_the_first_hint(self):
        hinted = base_payload()
        hinted["observations"].append(
            {"id": "note-hint-u", "case": "R-1", "root_hint": "U", "dropped": ["U", "V"]}
        )
        v_payload = json.loads(json.dumps(hinted))
        v_payload["drive_contact_chains"] = [chain("chain-v", "V", "V-latch-1", "R", "R-in-1")]
        v_case = relay_capture.import_data(self.db, v_payload)["cases"][0]
        self.assertEqual(v_case["owner"], "V")
        self.assertEqual(v_case["work_orders"][0]["evidence_refs"][0]["event_id"], "chain-v")

        other_db = connect(":memory:")
        try:
            u_payload = json.loads(json.dumps(hinted))
            u_payload["wiring_versions"] = [wiring("A", ["u-tech", "v-tech"])]
            u_payload["common_edge_records"] = [strip_u()]
            u_case = relay_capture.import_data(other_db, u_payload)["cases"][0]
            self.assertEqual(u_case["owner"], "U")
        finally:
            other_db.close()

    def test_later_original_re_adjudicates_without_erasing_the_old_direction_order(self):
        # First a complete, countersigned strip names V as the originator.
        strip_v = {
            "event_id": "strip-v",
            "case": "R-1",
            "version": "A",
            "signers": ["u-tech", "v-tech"],
            "window_complete": True,
            "cleared_after_window": False,
            "edges": [
                {"node": "V", "contact": "V-c1", "scale_tick": 9},
                {"node": "R", "contact": "R-c1", "scale_tick": 12},
                {"node": "U", "contact": "U-c2", "scale_tick": 15},
            ],
        }
        first = base_payload()
        first["wiring_versions"] = [wiring("A", ["u-tech", "v-tech"])]
        first["common_edge_records"] = [strip_v]
        relay_capture.import_data(self.db, first)
        before = self.verdict()
        self.assertEqual(before["owner"], "V")
        old_id = before["work_orders"][0]["id"]

        # A later authorized custody chain seals U's first-send while the old
        # strip still stands: the bench must stop at a named conflict and
        # leave the V order on file rather than silently rewriting it.
        relay_capture.import_data(self.db, {
            "drive_contact_chains": [chain("chain-u", "U", "U-latch-1", "R", "R-in-9")],
        })
        self.assertEqual(self.verdict()["state"], "evidence_conflict")
        self.assertEqual(
            [tuple(r) for r in self.db.execute(
                "SELECT id,owner,state FROM relay_work_order ORDER BY id")],
            [(old_id, "V", "open")],
        )

        # The authority then files a two-side retraction of the mistaken strip
        # (its bytes remain in evidence) and the U chain re-adjudicates cleanly.
        relay_capture.import_data(self.db, {
            "retractions": [{
                "event_id": "retract-strip-v",
                "case": "R-1",
                "target_event_id": "strip-v",
                "signers": ["u-tech", "v-tech"],
                "reason": "strip contact roles re-checked against sealed custody chain",
            }],
        })
        after = self.verdict()
        self.assertEqual(after["state"], "assigned")
        self.assertEqual(after["owner"], "U")
        orders = self.db.execute(
            "SELECT id,owner,state FROM relay_work_order ORDER BY id"
        ).fetchall()
        self.assertEqual([tuple(r) for r in orders], [
            (old_id, "V", "superseded"),
            ("WO-R-1-round-0-2", "U", "open"),
        ])
        # The retracted strip bytes are retained under the hash chain.
        self.assertIsNotNone(
            self.db.execute(
                "SELECT 1 FROM evidence WHERE event_id='strip-v'"
            ).fetchone()
        )

    def test_at_most_one_new_evidence_action_and_it_must_be_able_to_distinguish(self):
        case = self.import_case()
        action = case["action"]
        self.assertEqual(action["procedure"], "common-edge-recorder")
        self.assertEqual(self.verdict()["action"], action)

        relay_capture.import_data(self.db, {
            "common_edge_records": [{
                "event_id": "strip-late",
                "case": "R-1",
                "version": None,
                "signers": ["u-tech", "v-tech"],
                "window_complete": False,
                "cleared_after_window": True,
                "edges": [],
            }]
        })
        second = self.verdict()
        self.assertEqual(second["action"]["procedure"], "drive-contact-custody")

        relay_capture.import_data(self.db, {
            "drive_contact_chains": [
                chain("chain-broken", "U", "U-latch-1", "R", "R-in-1", damaged=True)
            ]
        })
        exhausted = self.verdict()
        self.assertEqual(exhausted["state"], "awaiting_source")
        self.assertIsNone(exhausted["action"])
        self.assertEqual(exhausted["work_orders"], [])

    def test_repeated_import_and_reconcile_are_deterministic(self):
        payload = base_payload()
        payload["wiring_versions"] = [wiring("A", ["u-tech", "v-tech"])]
        payload["common_edge_records"] = [strip_u()]
        first = relay_capture.import_data(self.db, payload)["cases"][0]
        second = relay_capture.import_data(self.db, json.loads(json.dumps(payload)))["cases"][0]
        self.assertEqual(first, second)
        logs_before = [tuple(r) for r in self.db.execute(
            "SELECT subsystem,object_id,state,reason,evidence_id FROM decision_log ORDER BY id"
        )]
        relay_capture.import_data(self.db, json.loads(json.dumps(payload)))
        logs_after = [tuple(r) for r in self.db.execute(
            "SELECT subsystem,object_id,state,reason,evidence_id FROM decision_log ORDER BY id"
        )]
        self.assertEqual(logs_before, logs_after)

    def test_single_side_retraction_cannot_remove_an_original(self):
        first = base_payload()
        first["wiring_versions"] = [wiring("A", ["u-tech", "v-tech"])]
        first["common_edge_records"] = [strip_u()]
        relay_capture.import_data(self.db, first)
        relay_capture.import_data(self.db, {
            "retractions": [{
                "event_id": "retract-lonely",
                "case": "R-1",
                "target_event_id": "strip-u",
                "signers": ["u-tech"],
                "reason": "one side disputes the strip without the other",
            }],
        })
        case = self.verdict()
        self.assertEqual(case["state"], "assigned")
        self.assertEqual(case["owner"], "U")

    def test_trustworthy_relative_order_cannot_replace_wiring_signoff(self):
        # A perfectly ordered strip is still unusable while its contact roles
        # depend on an unsigned wiring bitmap: relative order is not line proof.
        payload = base_payload()
        payload["wiring_versions"] = [wiring("A", ["u-tech"])]
        payload["common_edge_records"] = [strip_u()]
        case = relay_capture.import_data(self.db, payload)["cases"][0]
        self.assertIsNone(case["owner"])
        self.assertEqual(case["action"]["procedure"], "wiring-signoff")
        finding = relay_owner.evaluate_common_recorder(
            self.db, relay_owner.feasible_roots(self.db, "R-1")[1]
        )[0]
        self.assertEqual(finding["state"], "unusable")
        self.assertTrue(any("line sign-off" in reason for reason in finding["reasons"]))

    def test_relay_work_does_not_touch_energization_or_allocation_tables(self):
        self.import_case()
        for table in ("energize_permit", "power_allocation", "safety_inspection", "breaker_trip"):
            self.assertEqual(
                self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0
            )


class RelayCliReplayTest(unittest.TestCase):
    def test_cli_relay_import_replays_to_identical_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "relay.json"
            payload = base_payload()
            payload["wiring_versions"] = [wiring("A", ["u-tech", "v-tech"])]
            payload["common_edge_records"] = [strip_u()]
            source.write_text(json.dumps(payload), encoding="utf-8")
            db_path = tmp_path / "relay.sqlite"
            run = subprocess.run(
                [sys.executable, "-m", "boothpower", "relay-import",
                 "--db", str(db_path), "--input", str(source)],
                cwd=ROOT, capture_output=True, text=True, check=True,
            )
            result = json.loads(run.stdout)
            self.assertEqual(result["cases"][0]["owner"], "U")
            audit = subprocess.run(
                [sys.executable, "-m", "boothpower", "audit", "--db", str(db_path)],
                cwd=ROOT, capture_output=True, text=True, check=True,
            )
            report = json.loads(audit.stdout)
            self.assertTrue(report["replay_ok"], report)
            self.assertEqual(report["journal_count"], 1)


class RelayTopologyAndInputTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")

    def tearDown(self):
        self.db.close()

    def test_node_outside_u_r_v_is_rejected(self):
        payload = base_payload()
        payload["meter_readings"].append(
            {"event_id": "read-w", "case": "R-1", "node": "W",
             "contact": "W-edge", "reading": 10}
        )
        with self.assertRaises(ValueError):
            relay_capture.import_data(self.db, payload)

    def test_first_send_must_be_an_endpoint(self):
        payload = base_payload()
        payload["drive_contact_chains"] = [
            chain("chain-relay", "R", "R-latch", "U", "U-in")
        ]
        with self.assertRaises(ValueError):
            relay_capture.import_data(self.db, payload)

    def test_retraction_of_unknown_original_is_rejected(self):
        payload = base_payload()
        payload["retractions"] = [{
            "event_id": "retract-ghost",
            "case": "R-1",
            "target_event_id": "does-not-exist",
            "signers": ["u-tech", "v-tech"],
            "reason": "no such original",
        }]
        with self.assertRaises(ValueError):
            relay_capture.import_data(self.db, payload)

    def test_conflicting_reimport_of_same_event_id_is_rejected(self):
        relay_capture.import_data(self.db, base_payload())
        changed = base_payload()
        changed["meter_readings"][0]["reading"] = 4
        with self.assertRaises(ValueError):
            relay_capture.import_data(self.db, changed)

    def test_no_relay_command_or_reset_payload_is_accepted_as_evidence(self):
        # The capture module only understands evidence shapes; it never emits
        # device commands. An attempted command-shaped item fails validation.
        payload = base_payload()
        payload["commands"] = [{"event_id": "cmd-1", "case": "R-1", "action": "open-relay"}]
        result = relay_capture.import_data(self.db, payload)
        self.assertNotIn("commands", result["imported"])
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM breaker_trip").fetchone()[0], 0
        )


if __name__ == "__main__":
    unittest.main()
