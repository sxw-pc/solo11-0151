import json
from pathlib import Path

from boothpower import bench_capture
from boothpower.store import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_bench_observation_changes_calibration_verdict():
    db = connect(":memory:")
    try:
        data = json.loads((FIXTURES / "bench_comparisons.json").read_text(encoding="utf-8"))
        output = bench_capture.import_data(db, data)
        assert [row["state"] for row in output] == ["calibrated", "held"]
    finally:
        db.close()
