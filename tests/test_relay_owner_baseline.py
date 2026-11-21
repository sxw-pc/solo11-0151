import json
from pathlib import Path

from boothpower import relay_capture
from boothpower.store import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_current_notes_change_repair_destination_without_raw_edges():
    db = connect(":memory:")
    try:
        fixture = json.loads((FIXTURES / "relay-root-bench.json").read_text(encoding="utf-8"))
        out = relay_capture.import_data(db, fixture)
        assert [(r["state"], r["owner"]) for r in out] == [
            ("awaiting_source", None),
            ("assigned", "U"),
        ]
    finally:
        db.close()
