"""Route a relay investigation according to signed root hints."""

import json

from .store import log


def reconcile(db, case_id):
    observed = [
        json.loads(row["raw"])
        for row in db.execute("SELECT raw FROM evidence WHERE kind='relay_observation' ORDER BY rowid")
    ]
    hints = {row["root_hint"] for row in observed if row["case"] == case_id and row["root_hint"]}
    state = "assigned" if len(hints) == 1 else "awaiting_source"
    owner = next(iter(hints)) if len(hints) == 1 else None
    log(db, "relay", case_id, state, "signed source hints route the case")
    return {"case": case_id, "state": state, "owner": owner}
