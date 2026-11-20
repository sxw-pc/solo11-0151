import json
import unittest
from contextlib import closing
from pathlib import Path

from boothpower import circuit, declaration
from boothpower.store import connect

FIX = Path(__file__).resolve().parents[1] / "fixtures"


class ExchangeSeedTest(unittest.TestCase):
    def test_original_two_independent_requests_wait(self):
        case = json.loads((FIX / "exchange-witness.json").read_bytes())
        declared = json.loads((FIX / "declaration_valid.json").read_bytes())
        with closing(connect(":memory:")) as db:
            declaration.import_data(db, declared)
            initial = circuit.import_data(db, case["initial"])
            self.assertEqual([r["state"] for r in initial], ["confirmed", "confirmed"])
            outcomes = circuit.import_data(db, case["requested"])
            self.assertEqual([r["state"] for r in outcomes], ["waiting", "waiting"])
            self.assertTrue(all(r["reason"] == "overlapping peak exceeds breaker capacity"
                                for r in outcomes))
            current = db.execute("SELECT booth, circuit FROM power_allocation "
                                 "WHERE state='confirmed' ORDER BY booth").fetchall()
            self.assertEqual([tuple(r) for r in current], [("A", "C1"), ("B", "C2")])

