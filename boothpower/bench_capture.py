"""Local intake for reusable meter bench comparisons."""

from .bench_result import classify
from .source import record


def import_data(db, data):
    with db:
        results = []
        for sample in data["comparisons"]:
            record(db, sample["id"], "bench_comparison", sample)
            results.append(classify(db, sample))
    return results
