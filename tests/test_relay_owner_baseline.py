import json
import unittest
from pathlib import Path

from boothpower import relay_capture, relay_owner
from boothpower.store import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _load():
    return json.loads((FIXTURES / "relay-root-bench.json").read_text(encoding="utf-8"))


class RelayOwnerBaselineTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")

    def tearDown(self):
        self.db.close()

    def test_summary_hints_are_kept_as_claims_but_never_dispatch_a_work_order(self):
        fixture = _load()
        out = relay_capture.import_data(self.db, fixture)
        # Both coarse notes leave the case without an originating original:
        # the second note's signed root_hint is preserved but cannot by
        # itself assign the repair ticket.  The bench still holds one
        # collectable verification route, so the case is pending, not closed.
        self.assertEqual([c["state"] for c in out["cases"]], ["pending_verification"])
        case = out["cases"][0]
        self.assertIsNone(case["owner"])
        self.assertEqual(case["work_orders"], [])
        self.assertEqual(case["action"]["procedure"], "common-edge-recorder")

        # The original summary notes are retained untouched under hash storage.
        raw_ids = {
            row["event_id"]
            for row in self.db.execute(
                "SELECT event_id FROM evidence WHERE kind='relay_observation'"
            )
        }
        self.assertEqual(raw_ids, {"relay-note-1", "relay-note-2"})
        hints = {
            json.loads(row["raw"]).get("root_hint")
            for row in self.db.execute(
                "SELECT raw FROM evidence WHERE kind='relay_observation'"
            )
        }
        self.assertEqual(hints, {None, "U"})

        # The reconciliation rule itself no longer treats a hint as proof.
        self.assertEqual(relay_owner.reconcile(self.db, "R-1")[0]["state"],
                         "pending_verification")

    def test_legacy_fixture_keeps_both_physical_histories_open(self):
        out = relay_capture.import_data(self.db, _load())
        case = out["cases"][0]
        # U at 9 / V at 11 and U at 11 / V at 9 are both inside the signed
        # 2-4 tick band once each meter keeps its own [-2, 2] offset.
        self.assertEqual(case["compatible_roots"], ["U", "V"])


if __name__ == "__main__":
    unittest.main()
