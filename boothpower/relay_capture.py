"""Collect coarse notes for synthetic relay incidents."""

from .relay_owner import reconcile
from .source import record


def import_data(db, data):
    with db:
        results = []
        for observation in data["observations"]:
            record(db, observation["id"], "relay_observation", observation)
            results.append(reconcile(db, observation["case"]))
    return results
