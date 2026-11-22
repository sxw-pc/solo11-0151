"""Offline capture of synthetic relay incident originals.

Everything here is an import of evidence that already exists in the field:
no function in this module sends a command to a relay, changes energization,
resets a device or reassigns a circuit.  Re-importing the same original is
idempotent and an offline replay reaches the same verdicts.
"""

import json

from .relay_owner import NODES, ENDPOINTS, reconcile
from .source import record

_TOPOLOGY = set(NODES)
_DEFAULT_ROUND = "round-0"


def _case_of(item):
    case = item.get("case")
    if not case:
        raise ValueError("relay evidence requires a case id")
    return case


def _round_of(item):
    return item.get("round_id") or _DEFAULT_ROUND


def _check_node(node, event_id):
    if node not in _TOPOLOGY:
        raise ValueError(f"{event_id}: node {node} outside the supported U-R-V topology")


def _check_endpoint(node, event_id):
    if node not in ENDPOINTS:
        raise ValueError(f"{event_id}: {node} is not an endpoint U or V")


def _store_context(db, data, case_ids):
    clocks = data.get("clocks")
    ordering = data.get("permitted_ordering")
    options = data.get("local_evidence_options")
    if clocks is None and ordering is None and options is None:
        return
    for case in sorted(case_ids):
        # The fixture layout is a shared {"U": {...}, "V": {...}} map; a
        # per-case nested map is accepted as well.
        shared = clocks if clocks and all(node in clocks for node in ENDPOINTS) else clocks.get(case, {})
        round_id = data.get("round_id") or _DEFAULT_ROUND
        row = db.execute(
            "SELECT clocks_json, ordering_json, options_json FROM relay_case_context "
            "WHERE case_id=? AND round_id=?",
            (case, round_id),
        ).fetchone()
        payload = (
            _json(shared),
            _json(ordering or []),
            _json(options or []),
        )
        if row is None:
            db.execute(
                "INSERT INTO relay_case_context(case_id,round_id,clocks_json,ordering_json,options_json) "
                "VALUES(?,?,?,?,?)",
                (case, round_id, *payload),
            )
        elif (row["clocks_json"], row["ordering_json"], row["options_json"]) != payload:
            raise ValueError(f"case {case}: conflicting clock/wiring context for the round")


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def import_data(db, data):
    observations = data.get("observations", [])
    case_ids = {_case_of(item) for item in observations}
    for key in ("meter_readings", "wiring_versions", "common_edge_records",
                "drive_contact_chains", "retractions"):
        case_ids |= {_case_of(item) for item in data.get(key, [])}
    with db:
        _store_context(db, data, case_ids)
        imported = {"observations": 0, "meter_readings": 0, "wiring_versions": 0,
                    "common_edge_records": 0, "drive_contact_chains": 0, "retractions": 0}

        for note in observations:
            event_id = note["id"]
            if record(db, event_id, "relay_observation", note):
                imported["observations"] += 1
            # A root_hint is a signed claim on a coarse summary, not an
            # original contact: it is preserved but never drives dispatch.

        for reading in data.get("meter_readings", []):
            event_id = reading["event_id"]
            _check_node(reading["node"], event_id)
            if record(db, event_id, "relay_meter_reading", reading):
                db.execute(
                    "INSERT INTO relay_meter_reading(event_id,case_id,round_id,node,contact,"
                    "reading,missing) VALUES(?,?,?,?,?,?,?)",
                    (event_id, _case_of(reading), _round_of(reading), reading["node"],
                     reading.get("contact"), reading.get("reading"),
                     1 if reading.get("missing") else 0),
                )
                imported["meter_readings"] += 1

        for wiring in data.get("wiring_versions", []):
            event_id = wiring["event_id"]
            for line in wiring.get("lines", []):
                if line["role"] not in ("local_send", "device_echo"):
                    raise ValueError(f"{event_id}: wiring line role must be local_send or device_echo")
                _check_node(line["node"], event_id)
            if record(db, event_id, "wiring_attestation", wiring):
                db.execute(
                    "INSERT INTO wiring_attestation(event_id,case_id,round_id,version,signers_json,lines_json) "
                    "VALUES(?,?,?,?,?,?)",
                    (event_id, _case_of(wiring), _round_of(wiring), wiring["version"],
                     _json(wiring.get("signers", [])), _json(wiring.get("lines", []))),
                )
                imported["wiring_versions"] += 1

        for strip in data.get("common_edge_records", []):
            event_id = strip["event_id"]
            for edge in strip.get("edges", []):
                _check_node(edge["node"], event_id)
            if record(db, event_id, "common_edge_record", strip):
                db.execute(
                    "INSERT INTO common_edge_record(event_id,case_id,round_id,version,signers_json,"
                    "window_complete,cleared_after_window,edges_json) VALUES(?,?,?,?,?,?,?,?)",
                    (event_id, _case_of(strip), _round_of(strip), strip.get("version"),
                     _json(strip.get("signers", [])), 1 if strip.get("window_complete") else 0,
                     1 if strip.get("cleared_after_window") else 0,
                     _json(strip.get("edges", []))),
                )
                imported["common_edge_records"] += 1

        for chain in data.get("drive_contact_chains", []):
            event_id = chain["event_id"]
            for link in chain.get("links", []):
                _check_endpoint(link["send_node"], event_id)
                _check_node(link["receive_node"], event_id)
            if record(db, event_id, "drive_contact_chain", chain):
                db.execute(
                    "INSERT INTO drive_contact_chain(event_id,case_id,round_id,signers_json,links_json) "
                    "VALUES(?,?,?,?,?)",
                    (event_id, _case_of(chain), _round_of(chain),
                     _json(chain.get("signers", [])), _json(chain.get("links", []))),
                )
                imported["drive_contact_chains"] += 1

        for retraction in data.get("retractions", []):
            event_id = retraction["event_id"]
            target = retraction["target_event_id"]
            if not db.execute("SELECT 1 FROM evidence WHERE event_id=?", (target,)).fetchone():
                raise ValueError(f"{event_id}: retraction targets unknown original {target}")
            if record(db, event_id, "relay_retraction", retraction):
                db.execute(
                    "INSERT INTO relay_retraction(event_id,case_id,round_id,target_event_id,"
                    "signers_json,reason) VALUES(?,?,?,?,?,?)",
                    (event_id, _case_of(retraction), _round_of(retraction), target,
                     _json(retraction.get("signers", [])), retraction.get("reason", "")),
                )
                imported["retractions"] += 1

        verdicts = []
        for case in sorted(case_ids):
            verdicts.extend(reconcile(db, case))
    return {"imported": imported, "cases": verdicts}
