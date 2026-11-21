import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from boothpower import circuit, declaration, exchange, report
from boothpower.store import connect

FIX = Path(__file__).resolve().parents[1] / "fixtures"
ROOT = Path(__file__).resolve().parents[1]
MANIFEST = {"id": "ex-1", "applications": ["swap-A", "swap-B"]}


def load(name):
    return json.loads((FIX / name).read_bytes())


def boot(db, initial=None):
    """签核负荷 + 旧确认分配 + 两条互换申请（逐条提交只能候补）。"""
    declaration.import_data(db, load("declaration_valid.json"))
    case = load("exchange-witness.json")
    circuit.import_data(db, initial if initial is not None else case["initial"])
    out = circuit.import_data(db, case["requested"])
    assert [r["state"] for r in out] == ["waiting", "waiting"]
    return case


def confirmed(db):
    return {r["booth"]: r["circuit"] for r in db.execute(
        "SELECT booth, circuit FROM power_allocation WHERE state='confirmed'")}


def permits(db):
    return {r["booth"]: dict(r) for r in db.execute("SELECT * FROM energize_permit")}


def permit_circuits(db):
    rows = db.execute("SELECT p.booth, a.circuit FROM energize_permit p "
                      "JOIN power_allocation a ON a.id = p.allocation_id").fetchall()
    return {r["booth"]: r["circuit"] for r in rows}


def no_staging_initial(case):
    return {"circuits": [c for c in case["initial"]["circuits"] if c["circuit"] != "C3"],
            "applications": case["initial"]["applications"]}


class ExchangeWitnessTest(unittest.TestCase):
    def test_three_independent_shifts_complete_swap(self):
        with closing(connect(":memory:")) as db:
            boot(db)
            out = circuit.import_data(db, {"exchanges": [MANIFEST]})
            self.assertEqual([r["state"] for r in out], ["executing", "executing"])
            # 第一次独立移位之后：甲的许可指向 C3，乙仍在 C2
            self.assertEqual(confirmed(db), {"A": "C3", "B": "C2"})
            self.assertEqual(permit_circuits(db), {"A": "C3", "B": "C2"})
            st = exchange.status(db, "ex-1")
            self.assertEqual(st["state"], "executing")
            self.assertEqual(len(st["executed"]), 1)
            move = st["executed"][0]["moves"][0]
            self.assertEqual((move["booth"], move["from"], move["to"]), ("A", "C1", "C3"))
            self.assertEqual(move["old_allocation"], "old-A")
            self.assertEqual(move["old_circuit_event"], "exchange-v1-C1")
            self.assertEqual(move["new_circuit_event"], "exchange-v1-C3")
            self.assertEqual(len(st["remaining"]), 2)  # 未执行动作
            # 证据重放幂等，继续推进剩余两次独立移位
            circuit.import_data(db, {"exchanges": [MANIFEST]})
            self.assertEqual(confirmed(db), {"A": "C3", "B": "C1"})
            out = circuit.import_data(db, {"exchanges": [MANIFEST]})
            self.assertEqual(out[-1]["state"], "completed")
            self.assertEqual(confirmed(db), {"A": "C2", "B": "C1"})
            self.assertEqual(permit_circuits(db), {"A": "C2", "B": "C1"})
            st = exchange.status(db, "ex-1")
            self.assertEqual(st["state"], "completed")
            self.assertEqual(len(st["executed"]), 3)
            self.assertTrue(all(a["kind"] == "shift" for a in st["executed"]))
            # 旧分配与许可的版本归属
            final_a = db.execute("SELECT * FROM power_allocation WHERE booth='A' AND state='confirmed'").fetchone()
            self.assertEqual(final_a["circuit_event"], "exchange-v1-C2")
            self.assertEqual(final_a["decl_version"], 1)
            old_a = db.execute("SELECT * FROM power_allocation WHERE id='old-A'").fetchone()
            self.assertEqual(old_a["state"], "released")
            self.assertEqual(old_a["circuit_event"], "exchange-v1-C1")
            swap_a = db.execute("SELECT * FROM power_allocation WHERE id='swap-A'").fetchone()
            self.assertEqual(swap_a["state"], "released")
            self.assertIn("fulfilled by exchange ex-1", swap_a["reason"])
            # 报告包含状态路径与动作原件
            data = report.build(db)
            self.assertEqual(data["exchange_status"][0]["state"], "completed")
            self.assertEqual(len(data["exchange_action"]), 3)

    def test_without_staging_independent_only_refuses(self):
        case = load("exchange-witness.json")
        with closing(connect(":memory:")) as db:
            boot(db, no_staging_initial(case))
            out = circuit.import_data(db, {"exchanges": [MANIFEST]})
            self.assertEqual(out[-1]["state"], "blocked")
            st = exchange.status(db, "ex-1")
            self.assertEqual(st["state"], "blocked")
            # 只签最终表是不允许的：旧确认分配原样保留
            self.assertEqual(confirmed(db), {"A": "C1", "B": "C2"})
            self.assertEqual(st["executed"], [])
            # 阻碍环：实际占用互相指向
            cycle = {(c["booth"], c["holds"], c["target"], c["target_held_by"]) for c in st["cycle"]}
            self.assertEqual(cycle, {("A", "C1", "C2", "B"), ("B", "C2", "C1", "A")})
            # 缺少的凭据
            self.assertTrue(any("staging" in m for m in st["missing"]))
            self.assertTrue(any("group permit" in m for m in st["missing"]))
            # 成员申请被换位接管，不会被单独晋升签出最终表
            self.assertEqual(db.execute("SELECT state FROM power_allocation WHERE id='swap-A'").fetchone()[0], "held")
            self.assertEqual(permit_circuits(db), {"A": "C1", "B": "C2"})

    def test_group_permit_allows_atomic_swap(self):
        with closing(connect(":memory:")) as db:
            case = boot(db)
            permit = case["exclusive_mock_group"]
            receipt = exchange.device_sign_group(permit, {"A": "C2", "B": "C1"}, "2026-11-17T09:00:00")
            self.assertIsNotNone(receipt)
            out = circuit.import_data(db, {"group_permits": [permit],
                                           "device_receipts": [receipt],
                                           "exchanges": [MANIFEST]})
            self.assertEqual(out[-1]["state"], "completed")
            st = exchange.status(db, "ex-1")
            self.assertEqual(st["state"], "completed")
            # 一次成组移位直接产出 C1/C2 -> C2/C1 的完整新版本
            self.assertEqual(len(st["executed"]), 1)
            action = st["executed"][0]
            self.assertEqual(action["kind"], "group")
            self.assertEqual(action["receipt"], receipt["id"])
            moves = {(m["booth"], m["from"], m["to"]) for m in action["moves"]}
            self.assertEqual(moves, {("A", "C1", "C2"), ("B", "C2", "C1")})
            self.assertEqual(confirmed(db), {"A": "C2", "B": "C1"})
            self.assertEqual(permit_circuits(db), {"A": "C2", "B": "C1"})
            final_b = db.execute("SELECT * FROM power_allocation WHERE booth='B' AND state='confirmed'").fetchone()
            self.assertEqual(final_b["circuit_event"], "exchange-v1-C1")

    def test_partial_group_receipt_not_admitted(self):
        case = load("exchange-witness.json")
        with closing(connect(":memory:")) as db:
            boot(db, no_staging_initial(case))
            permit = case["exclusive_mock_group"]
            # 模拟设备拒签部分成员；现场只留下覆盖甲的半张回执
            self.assertIsNone(exchange.device_sign_group(permit, {"A": "C2"}, "2026-11-17T09:00:00"))
            partial = {"id": "rcpt-partial-1", "kind": "group", "permit": permit["id"],
                       "members": ["A"], "moves": {"A": "C2"},
                       "device": "synthetic-controller", "at": "2026-11-17T09:00:00"}
            out = circuit.import_data(db, {"group_permits": [permit],
                                           "device_receipts": [partial],
                                           "exchanges": [MANIFEST]})
            self.assertEqual(out[-1]["state"], "blocked")
            st = exchange.status(db, "ex-1")
            # 不得出现半个成组换位
            self.assertEqual(st["executed"], [])
            self.assertEqual(confirmed(db), {"A": "C1", "B": "C2"})
            self.assertEqual(permit_circuits(db), {"A": "C1", "B": "C2"})
            self.assertTrue(any("complete device receipt" in m for m in st["missing"]))

    def test_same_time_separate_receipts_not_simultaneous(self):
        case = load("exchange-witness.json")
        with closing(connect(":memory:")) as db:
            boot(db, no_staging_initial(case))
            permit = case["exclusive_mock_group"]
            at = "2026-11-17T09:00:00"
            separate = [
                {"id": "rcpt-only-A", "kind": "group", "permit": permit["id"],
                 "members": ["A"], "moves": {"A": "C2"}, "device": "synthetic-controller", "at": at},
                {"id": "rcpt-only-B", "kind": "group", "permit": permit["id"],
                 "members": ["B"], "moves": {"B": "C1"}, "device": "synthetic-controller", "at": at},
            ]
            out = circuit.import_data(db, {"group_permits": [permit],
                                           "device_receipts": separate,
                                           "exchanges": [MANIFEST]})
            self.assertEqual(out[-1]["state"], "blocked")
            st = exchange.status(db, "ex-1")
            self.assertEqual(st["executed"], [])
            self.assertEqual(confirmed(db), {"A": "C1", "B": "C2"})
            self.assertTrue(any("complete device receipt" in m for m in st["missing"]))

    def test_capacity_rebooking_replans_from_witnessed_position(self):
        with closing(connect(":memory:")) as db:
            boot(db)
            circuit.import_data(db, {"exchanges": [MANIFEST]})
            self.assertEqual(confirmed(db), {"A": "C3", "B": "C2"})
            # 甲已到暂留 C3 后，C2 被另一笔新版授权限制
            circuit.import_data(db, {
                "circuits": [{"event_id": "exchange-v2-C2", "version": 2, "circuit": "C2",
                              "phase": "L2", "capacity_kw": 2,
                              "start": "2026-11-17T08:00:00", "end": "2026-11-17T18:00:00"}],
                "applications": [{"id": "alloc-F1", "booth": "F", "circuit": "C2", "kw": 2,
                                  "start": "2026-11-17T09:00:00", "end": "2026-11-17T10:00:00",
                                  "priority": 5, "formal": False}]})
            current = confirmed(db)
            # 已到暂留位的甲不一键回到旧表
            self.assertEqual(current["A"], "C3")
            self.assertEqual(current["F"], "C2")
            self.assertNotIn("B", current)
            self.assertEqual(db.execute("SELECT state FROM power_allocation WHERE id='old-B'").fetchone()[0],
                             "waiting")
            st = exchange.status(db, "ex-1")
            self.assertEqual(st["state"], "blocked")
            self.assertEqual(len(st["executed"]), 1)  # 已实证动作保留
            self.assertTrue(any("participant B" in m for m in st["missing"]))
            self.assertTrue(any("C2" in m and "capacity" in m for m in st["missing"]))
            held_by = {c["booth"]: c["target_held_by"] for c in st["cycle"]}
            self.assertEqual(held_by["A"], "F")
            # 许可与当前物理分配一致
            self.assertEqual(permit_circuits(db)["A"], "C3")
            perm_b = permits(db)["B"]
            self.assertEqual(perm_b["state"], "blocked")
            self.assertIsNone(perm_b["allocation_id"])

    def test_restart_reimport_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "state.sqlite")
            with closing(connect(path)) as db:
                boot(db)
                for _ in range(3):
                    circuit.import_data(db, {"exchanges": [MANIFEST]})
                self.assertEqual(exchange.status(db, "ex-1")["state"], "completed")
                before = report.build(db)
            # 重启后重复导入全部证据：账目不变
            with closing(connect(path)) as db:
                declaration.import_data(db, load("declaration_valid.json"))
                case = load("exchange-witness.json")
                circuit.import_data(db, case["initial"])
                out = circuit.import_data(db, case["requested"])
                self.assertTrue(all(r["state"] == "idempotent" for r in out))
                circuit.import_data(db, {"exchanges": [MANIFEST]})
                after = report.build(db)
                self.assertEqual(before, after)
                self.assertEqual(len(after["exchange_action"]), 3)
                self.assertEqual(confirmed(db), {"A": "C2", "B": "C1"})

    def test_two_exchanges_compete_for_one_staging(self):
        decl = {"contracts": [{"event_id": f"contract-{b}", "booth": b,
                               "start": "2026-11-17T08:00:00", "end": "2026-11-17T18:00:00",
                               "signer": "venue"} for b in "ABDE"],
                "declarations": [{"event_id": f"load-{b}1", "booth": b, "version": 1, "kw": 3,
                                  "at": f"2026-11-17T08:3{i}:00", "approved_by": "electrician",
                                  "devices": ["light"]} for i, b in enumerate("ABDE")]}
        def cv(name, phase, end):
            return {"event_id": f"cv-{name}", "version": 1, "circuit": name, "phase": phase,
                    "capacity_kw": 3, "start": "2026-11-17T08:00:00", "end": end}
        def app(aid, booth, circ, start, end):
            return {"id": aid, "booth": booth, "circuit": circ, "kw": 3,
                    "start": start, "end": end, "priority": 1, "formal": True}
        initial = {"circuits": [cv("C1", "L1", "2026-11-17T10:00:00"),
                                cv("C2", "L2", "2026-11-17T10:00:00"),
                                cv("C3", "L3", "2026-11-17T18:00:00"),
                                cv("C4", "L4", "2026-11-17T18:00:00"),
                                cv("C5", "L5", "2026-11-17T18:00:00")],
                   "applications": [app("old-A", "A", "C1", "2026-11-17T09:00:00", "2026-11-17T10:00:00"),
                                    app("old-B", "B", "C2", "2026-11-17T09:00:00", "2026-11-17T10:00:00"),
                                    app("old-D", "D", "C4", "2026-11-17T09:00:00", "2026-11-17T11:00:00"),
                                    app("old-E", "E", "C5", "2026-11-17T09:00:00", "2026-11-17T11:00:00")]}
        requested = {"applications": [app("swap-A", "A", "C2", "2026-11-17T09:00:00", "2026-11-17T10:00:00"),
                                      app("swap-B", "B", "C1", "2026-11-17T09:00:00", "2026-11-17T10:00:00"),
                                      app("swap-D", "D", "C5", "2026-11-17T09:00:00", "2026-11-17T11:00:00"),
                                      app("swap-E", "E", "C4", "2026-11-17T09:00:00", "2026-11-17T11:00:00")]}
        manifests = {"exchanges": [{"id": "ex-1", "applications": ["swap-A", "swap-B"]},
                                   {"id": "ex-2", "applications": ["swap-D", "swap-E"]}]}
        with closing(connect(":memory:")) as db:
            declaration.import_data(db, decl)
            circuit.import_data(db, initial)
            out = circuit.import_data(db, requested)
            self.assertEqual([r["state"] for r in out], ["waiting"] * 4)
            circuit.import_data(db, manifests)
            # ex-1 先拿到唯一暂留 C3；ex-2 被同一暂留资源挡住
            self.assertEqual(confirmed(db)["A"], "C3")
            st2 = exchange.status(db, "ex-2")
            self.assertEqual(st2["state"], "blocked")
            self.assertTrue(any("C3" in m and "A" in m for m in st2["missing"]))
            self.assertEqual(confirmed(db)["D"], "C4")
            circuit.import_data(db, {})  # ex-1: B->C1；ex-2 仍被占住的 C3 挡住
            self.assertEqual(exchange.status(db, "ex-2")["state"], "blocked")
            circuit.import_data(db, {})  # ex-1: A->C2 完成并释放 C3；ex-2: D->C3
            self.assertEqual(exchange.status(db, "ex-1")["state"], "completed")
            self.assertEqual(confirmed(db)["D"], "C3")
            circuit.import_data(db, {})  # ex-2: E->C4
            out = circuit.import_data(db, {})  # ex-2: D->C5 完成
            self.assertEqual(out[-1]["state"], "completed")
            self.assertEqual(confirmed(db), {"A": "C2", "B": "C1", "D": "C5", "E": "C4"})
            self.assertEqual(permit_circuits(db), {"A": "C2", "B": "C1", "D": "C5", "E": "C4"})
            self.assertEqual(exchange.status(db, "ex-2")["state"], "completed")

    def test_exchange_plan_fixture_replays_via_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "cli.sqlite")
            run = subprocess.run([sys.executable, "-m", "boothpower", "exchange-run",
                                  "--db", path, "--input", str(FIX / "exchange-plan.json")],
                                 cwd=ROOT, capture_output=True, text=True, check=True)
            result = json.loads(run.stdout)
            self.assertTrue(result["audit"]["replay_ok"])
            ex = result["exchanges"][0]
            self.assertEqual(ex["state"], "completed")
            self.assertEqual(len(ex["executed"]), 3)
            self.assertEqual(ex["positions"], {"A": "C2", "B": "C1"})
            self.assertEqual(ex["permits"]["A"]["allocation_id"], "ex-1-a3-A")


if __name__ == "__main__":
    unittest.main()
