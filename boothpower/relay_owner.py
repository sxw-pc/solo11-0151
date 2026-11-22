"""Attribute synthetic relay trips from original field evidence.

The workbench never commands the devices: every input is an offline copy of a
signed original (a coarse note, a meter reading, a common-edge strip or a
sealed drive-contact chain).  Two physical histories can leave the same
"both relays dropped" summary, so a signed ``root_hint`` on a summary note is
kept as a claim but never signs a work order by itself.

Two independent attestation routes can prove the originating end:

* ``common-edge-recorder`` keeps the edges and physical contacts signed by
  both sides on one shared strip.  A complete window allows a single common
  scale and checking the propagation sequence against the round's wiring.
  Each sub-file rides the strip's clock, so no separate clock proof is asked
  of it.  When the old contacts were already cleared, a dark lamp after the
  window cannot attribute the trip.
* ``drive-contact-custody`` binds the first-send latch to the receiving
  original of the same drive, countersigned by both sides.  Neither side's
  clock is trusted and a burnt strip does not invalidate the sealed
  contacts; a mirrored copy of a shared drive is never a second origination.

At most one new evidence-gathering action is ever recommended, and a work
order is only assigned when an original actually proves one originating end.
"""

import json

from .store import log

ENDPOINTS = ("U", "V")
NODES = ("U", "R", "V")
MIN_GAP = 2
MAX_GAP = 4


def _loads(value, default=None):
    return json.loads(value) if value is not None else default


# ---------------------------------------------------------------------------
# Feasibility from meter readings
# ---------------------------------------------------------------------------

def feasible_roots(db, case_id, round_id=None):
    """Root directions still compatible with the round's readable observations.

    Every node keeps one offset for the whole round, chosen inside its signed
    ``[-2, 2]`` band.  A missing reading only removes a constraint: it may be
    a blank seen after the device was reset and never proves an old contact
    stayed dark.  Each traversed segment must consume 2-4 common-scale ticks.
    File arrival order is never used as timing evidence.
    """
    if round_id is None:
        context = db.execute(
            "SELECT * FROM relay_case_context WHERE case_id=? ORDER BY round_id DESC LIMIT 1",
            (case_id,),
        ).fetchone()
    else:
        context = db.execute(
            "SELECT * FROM relay_case_context WHERE case_id=? AND round_id=?",
            (case_id, round_id),
        ).fetchone()
    if context is None:
        return set(), None
    ctx = {
        "case_id": case_id,
        "round_id": context["round_id"],
        "clocks": _loads(context["clocks_json"], {}),
        "ordering": _loads(context["ordering_json"], []),
        "options": _loads(context["options_json"], []),
    }
    rows = db.execute(
        "SELECT * FROM relay_meter_reading WHERE case_id=? AND round_id=? ORDER BY event_id",
        (case_id, ctx["round_id"]),
    ).fetchall()
    readings = {node: [] for node in NODES}
    for row in rows:
        readings.setdefault(row["node"], []).append(row)
    feasible = {root for root in ENDPOINTS if _root_fits(root, readings, ctx)}
    return feasible, ctx


def _root_fits(root, readings, ctx):
    path = ("U", "R", "V") if root == "U" else ("V", "R", "U")
    domains = {}
    edge_tick = {}
    for node in NODES:
        rows = readings.get(node, [])
        observed = [r["reading"] for r in rows if not r["missing"] and r["reading"] is not None]
        lo, hi = (ctx["clocks"].get(node) or {}).get("offset_bounds", [-2, 2])
        domains[node] = list(range(lo, hi + 1))
        edge_tick[node] = min(observed) if observed else None
    for o_a in domains[path[0]]:
        for o_b in domains[path[1]]:
            for o_c in domains[path[2]]:
                offsets = {path[0]: o_a, path[1]: o_b, path[2]: o_c}
                common = {}
                for node in NODES:
                    if edge_tick[node] is None:
                        continue  # blank/missing reading cannot veto a direction
                    common[node] = edge_tick[node] - offsets[node]
                if _segments_inside_band(path, common):
                    return True
    return False


def _segments_inside_band(path, common):
    for src, dst in zip(path, path[1:]):
        if src not in common or dst not in common:
            continue  # gap unobservable on this segment; neither direction is vetoed
        gap = common[dst] - common[src]
        if not (MIN_GAP <= gap <= MAX_GAP):
            return False
    return True


# ---------------------------------------------------------------------------
# Signed originals
# ---------------------------------------------------------------------------

def _signed_wiring(db, case_id, round_id):
    rows = db.execute(
        "SELECT * FROM wiring_attestation WHERE case_id=? AND round_id=? ORDER BY event_id",
        (case_id, round_id),
    ).fetchall()
    signed = [row for row in rows if len(set(_loads(row["signers_json"], []))) >= 2]
    return rows, signed


def _wiring_conflict(signed_wiring):
    """Two fully signed bitmap versions cannot assign the same physical
    contact to different local-send/echo roles: that is itself a conflict."""
    roles = {}
    for row in signed_wiring:
        for line in _loads(row["lines_json"], []):
            key = (line["node"], line["contact"])
            if key in roles and roles[key] != line["role"]:
                return {
                    "procedure": "wiring-signoff",
                    "event_id": row["event_id"],
                    "reasons": [
                        f"signed wiring versions disagree on the role of {line['node']} "
                        f"contact {line['contact']}: {roles[key]} versus {line['role']}"
                    ],
                }
            roles[key] = line["role"]
    return None


def _retracted(db, case_id, round_id):
    rows = db.execute(
        "SELECT * FROM relay_retraction WHERE case_id=? AND round_id=?",
        (case_id, round_id),
    ).fetchall()
    targets = set()
    for row in rows:
        # Only a two-side signed retraction removes an original from
        # adjudication; the retracted bytes themselves stay in evidence.
        if len(set(_loads(row["signers_json"], []))) >= 2:
            targets.add(row["target_event_id"])
    return targets


def evaluate_common_recorder(db, ctx):
    retracted = _retracted(db, ctx["case_id"], ctx["round_id"])
    rows = db.execute(
        "SELECT * FROM common_edge_record WHERE case_id=? AND round_id=? ORDER BY event_id",
        (ctx["case_id"], ctx["round_id"]),
    ).fetchall()
    return [_evaluate_strip(row, ctx, db) for row in rows if row["event_id"] not in retracted]


def _evaluate_strip(row, ctx, db):
    signers = set(_loads(row["signers_json"], []))
    edges = _loads(row["edges_json"], [])
    finding = {
        "event_id": row["event_id"],
        "procedure": "common-edge-recorder",
        "round_id": ctx["round_id"],
        "state": "unusable",
        "roots": set(),
        "contacts": [],
        "reasons": [],
    }
    if len(signers) < 2:
        finding["reasons"].append("strip lacks countersignatures from both sides")
        return finding
    if not row["window_complete"]:
        finding["reasons"].append(
            "observation window incomplete; the full propagation sequence was not captured"
        )
        return finding
    if row["cleared_after_window"]:
        finding["reasons"].append(
            "old contacts were cleared before the spare strip captured them; "
            "a post-window dark lamp cannot attribute the trip"
        )
        return finding
    _, signed_wiring = _signed_wiring(db, ctx["case_id"], ctx["round_id"])
    referenced = next((w for w in signed_wiring if w["version"] == row["version"]), None)
    if row["version"] is None or referenced is None:
        finding["reasons"].append(
            "strip contact roles depend on a wiring bitmap whose two-side "
            "local-send/echo line sign-off is missing"
        )
        return finding
    if len(edges) < 2:
        finding["reasons"].append("strip does not hold a complete edge sequence")
        return finding
    ordered = sorted(edges, key=lambda edge: edge["scale_tick"])
    gaps = [b["scale_tick"] - a["scale_tick"] for a, b in zip(ordered, ordered[1:])]
    if any(not (MIN_GAP <= gap <= MAX_GAP) for gap in gaps):
        finding["state"] = "conflict"
        finding["reasons"].append(
            "edge ticks "
            + str([edge["scale_tick"] for edge in ordered])
            + " violate the signed 2-4 tick propagation band"
        )
        return finding
    role_by_contact = {
        line["contact"]: line["role"] for line in _loads(referenced["lines_json"], [])
    }
    first = ordered[0]
    if role_by_contact.get(first["contact"]) != "local_send":
        finding["state"] = "conflict"
        finding["reasons"].append(
            f"first edge contact {first['contact']} is not a signed local-send contact "
            f"under wiring {row['version']}"
        )
        return finding
    root = first["node"]
    if root not in ENDPOINTS:
        finding["state"] = "conflict"
        finding["reasons"].append("first edge does not originate at endpoint U or V")
        return finding
    finding["state"] = "proves"
    finding["roots"] = {root}
    finding["contacts"] = [
        {"node": edge["node"], "contact": edge["contact"], "scale_tick": edge["scale_tick"]}
        for edge in ordered
    ]
    finding["reasons"].append(
        f"complete two-side strip on signed wiring {row['version']}: {root} local-send "
        f"contact {first['contact']} leads a 2-4 tick propagation sequence across one common scale"
    )
    return finding


def evaluate_drive_chain(db, ctx):
    retracted = _retracted(db, ctx["case_id"], ctx["round_id"])
    rows = db.execute(
        "SELECT * FROM drive_contact_chain WHERE case_id=? AND round_id=? ORDER BY event_id",
        (ctx["case_id"], ctx["round_id"]),
    ).fetchall()
    return [_evaluate_chain(row, ctx) for row in rows if row["event_id"] not in retracted]


def _evaluate_chain(row, ctx):
    signers = set(_loads(row["signers_json"], []))
    links = _loads(row["links_json"], [])
    finding = {
        "event_id": row["event_id"],
        "procedure": "drive-contact-custody",
        "round_id": ctx["round_id"],
        "state": "unusable",
        "roots": set(),
        "contacts": [],
        "reasons": [],
    }
    if len(signers) < 2:
        finding["reasons"].append("custody chain lacks countersignatures from both sides")
        return finding
    usable = []
    for link in links:
        drive = link.get("drive_id")
        if link.get("mirror"):
            finding["reasons"].append(
                f"drive {drive}: mirrored copy of the shared drive ignored as an independent origination"
            )
            continue
        if link.get("send_contact_damaged"):
            finding["reasons"].append(
                f"drive {drive}: send contact {link.get('send_contact')} is locally damaged; "
                "the link cannot sign an originator"
            )
            continue
        if link.get("send_node") not in ENDPOINTS or link.get("receive_node") not in NODES:
            finding["reasons"].append(f"drive {drive}: link endpoints outside the U-R-V topology")
            continue
        if link["send_node"] == link["receive_node"]:
            finding["reasons"].append(f"drive {drive}: send and receive originals are the same node")
            continue
        usable.append(link)
    if not usable:
        return finding
    roots = {link["send_node"] for link in usable}
    if len(roots) > 1:
        finding["state"] = "conflict"
        finding["reasons"].append(
            "sealed chains name first-send latches at both endpoints ("
            + ", ".join(sorted(roots))
            + "); one physical drive cannot originate twice"
        )
        return finding
    root = next(iter(roots))
    bound = any(link["send_node"] == root for link in usable)
    if not bound:
        finding["reasons"].append("first-send latch is not bound to a countersigned receiving original")
        return finding
    finding["state"] = "proves"
    finding["roots"] = {root}
    finding["contacts"] = [
        {
            "drive_id": link["drive_id"],
            "send_node": link["send_node"],
            "send_contact": link["send_contact"],
            "receive_node": link["receive_node"],
            "receive_contact": link["receive_contact"],
        }
        for link in usable
    ]
    finding["reasons"].append(
        "two-side custody chain binds "
        + root
        + " first-send latch "
        + ", ".join(sorted({link["send_contact"] for link in usable}))
        + " to its receiving original without comparing either clock"
    )
    return finding


# ---------------------------------------------------------------------------
# Reconciliation and the single allowed verification action
# ---------------------------------------------------------------------------

def reconcile(db, case_id, round_id=None):
    context_rows = db.execute(
        "SELECT round_id FROM relay_case_context WHERE case_id=? ORDER BY round_id",
        (case_id,),
    ).fetchall()
    if not context_rows:
        return [{"case": case_id, "state": "awaiting_source", "owner": None,
                 "compatible_roots": [], "work_orders": [], "action": None,
                 "conflicts": [], "round_id": None}]
    rounds = [row["round_id"] for row in context_rows]
    if round_id is not None:
        rounds = [r for r in rounds if r == round_id] or [round_id]
    return [_reconcile_round(db, case_id, rid) for rid in rounds]


def _reconcile_round(db, case_id, round_id):
    compatible, ctx = feasible_roots(db, case_id, round_id)
    if ctx is None:
        return {"case": case_id, "state": "awaiting_source", "owner": None,
                "compatible_roots": [], "work_orders": [], "action": None,
                "conflicts": [], "round_id": round_id}
    findings = evaluate_common_recorder(db, ctx) + evaluate_drive_chain(db, ctx)

    _, signed_wiring = _signed_wiring(db, ctx["case_id"], ctx["round_id"])
    wiring_conflict = _wiring_conflict(signed_wiring)

    conflicts, proven = [], set()
    if wiring_conflict:
        wiring_conflict["round_id"] = ctx["round_id"]
        conflicts.append(wiring_conflict)
    for finding in findings:
        if finding["state"] == "conflict":
            conflicts.append({
                "procedure": finding["procedure"],
                "event_id": finding["event_id"],
                "round_id": ctx["round_id"],
                "reasons": finding["reasons"],
            })
        elif finding["state"] == "proves":
            proven |= finding["roots"]
    # Two independently sealed originals (possibly of different procedures)
    # cannot name different first-send endpoints for one physical history.
    if len(proven) > 1:
        refs = sorted(
            f"{finding['procedure']}:{finding['event_id']}"
            for finding in findings
            if finding["state"] == "proves"
        )
        conflicts.append({
            "procedure": "cross-procedure",
            "event_id": None,
            "round_id": ctx["round_id"],
            "reasons": [
                "originals "
                + ", ".join(refs)
                + " prove different originating endpoints; the named contacts cannot belong to one physical history"
            ],
        })

    if conflicts:
        state, owner, reason, action = (
            "evidence_conflict", None,
            "originals contradict each other; resolve the named contact/time conflict before dispatch",
            None,
        )
    elif len(proven) == 1:
        owner = next(iter(proven))
        state, reason, action = (
            "assigned",
            "a countersigned original proves the originating endpoint and its physical contact",
            None,
        )
    else:
        owner = None
        action = _choose_action(db, ctx, compatible)
        if action is None:
            state, reason = (
                "awaiting_source",
                "no original proves an originator and no remaining evidence route can distinguish the directions",
            )
        else:
            state, reason = (
                "pending_verification",
                "readable observations keep both directions compatible; one verification action is still collectable",
            )

    return _persist(db, case_id, ctx["round_id"], state, owner, reason,
                    sorted(compatible), action, conflicts, findings)


def _choose_action(db, ctx, compatible):
    prior = db.execute(
        "SELECT action_json FROM relay_case WHERE case_id=? AND round_id=?",
        (ctx["case_id"], ctx["round_id"]),
    ).fetchone()
    wiring_rows, signed_wiring = _signed_wiring(db, ctx["case_id"], ctx["round_id"])
    options = set(ctx["options"])
    strip_seen = db.execute(
        "SELECT 1 FROM common_edge_record WHERE case_id=? AND round_id=?",
        (ctx["case_id"], ctx["round_id"]),
    ).fetchone()
    chain_seen = db.execute(
        "SELECT 1 FROM drive_contact_chain WHERE case_id=? AND round_id=?",
        (ctx["case_id"], ctx["round_id"]),
    ).fetchone()
    if prior and prior["action_json"]:
        pending = _loads(prior["action_json"])
        # A previously recommended action stays open only until its own
        # original arrives; then that route counts as exercised and the
        # single allowed follow-up may target the other procedure.
        arrived = (
            (pending["procedure"] == "common-edge-recorder" and strip_seen)
            or (pending["procedure"] == "drive-contact-custody" and chain_seen)
            or (pending["procedure"] == "wiring-signoff" and signed_wiring)
        )
        if not arrived:
            return pending
    wiring_blocked = bool(wiring_rows) and not signed_wiring
    # When contact roles already in hand cannot be read without a line
    # sign-off, no relative order substitutes for it: ask for the sign-off.
    if wiring_blocked:
        return {
            "procedure": "wiring-signoff",
            "round_id": ctx["round_id"],
            "target_contacts": [
                {"node": node, "contact": f"{node}-local-send-line", "state": "unassigned"}
                for node in ENDPOINTS
            ],
            "reason": "two wiring bitmap versions remain unsigned; attribution depends on "
                      "which line is local-send versus device echo",
        }
    if "common-edge-recorder" in options and not strip_seen:
        return {
            "procedure": "common-edge-recorder",
            "round_id": ctx["round_id"],
            "target_contacts": [
                {"node": node, "contact": f"{node}-local-send", "state": "unknown"}
                for node in ENDPOINTS
            ],
            "reason": "capture the complete edge window on a spare common strip before contacts are cleared",
        }
    if "drive-contact-custody" in options and not chain_seen:
        return {
            "procedure": "drive-contact-custody",
            "round_id": ctx["round_id"],
            "target_contacts": [
                {"node": node, "contact": f"{node}-first-send-latch", "state": "unknown"}
                for node in sorted(compatible or ENDPOINTS)
            ],
            "reason": "seal the first-send latch with its receiving original under two-side "
                      "custody; the clocks and any burnt strip are irrelevant",
        }
    return None


def _persist(db, case_id, round_id, state, owner, reason, compatible, action, conflicts, findings):
    prior = db.execute(
        "SELECT * FROM relay_case WHERE case_id=? AND round_id=?",
        (case_id, round_id),
    ).fetchone()
    action_json = json.dumps(action, sort_keys=True, ensure_ascii=False) if action else None
    conflicts_json = json.dumps(conflicts, sort_keys=True, ensure_ascii=False)
    fingerprint = (state, owner, reason, action_json, conflicts_json)
    if prior is None or (prior["state"], prior["owner"], prior["reason"],
                         prior["action_json"], prior["conflicts_json"]) != fingerprint:
        version = (prior["version"] + 1) if prior else 1
        db.execute(
            "INSERT INTO relay_case(case_id,round_id,state,owner,reason,compatible_json,action_json,"
            "conflicts_json,version) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(case_id,round_id) DO UPDATE SET "
            "state=excluded.state,owner=excluded.owner,reason=excluded.reason,"
            "compatible_json=excluded.compatible_json,action_json=excluded.action_json,"
            "conflicts_json=excluded.conflicts_json,version=excluded.version",
            (case_id, round_id, state, owner, reason, json.dumps(compatible), action_json,
             conflicts_json, version),
        )
        log(db, "relay", f"{case_id}/{round_id}", state, reason)
    if state == "assigned":
        _maintain_order(db, case_id, round_id, owner, findings)
    orders = db.execute(
        "SELECT * FROM relay_work_order WHERE case_id=? AND round_id=? AND state='open' ORDER BY id",
        (case_id, round_id),
    ).fetchall()
    work_orders = []
    for order in orders:
        work_orders.append({
            "id": order["id"],
            "owner": order["owner"],
            "state": order["state"],
            "basis": _loads(order["basis_json"], []),
            "evidence_refs": _loads(order["evidence_json"], []),
        })
    return {
        "case": case_id,
        "round_id": round_id,
        "state": state,
        "owner": owner,
        "compatible_roots": compatible,
        "work_orders": work_orders,
        "action": action,
        "conflicts": conflicts,
    }


def _maintain_order(db, case_id, round_id, owner, findings):
    open_orders = db.execute(
        "SELECT * FROM relay_work_order WHERE case_id=? AND round_id=? AND state='open' ORDER BY id",
        (case_id, round_id),
    ).fetchall()
    if any(order["owner"] == owner for order in open_orders):
        return
    for order in open_orders:
        db.execute(
            "UPDATE relay_work_order SET state='superseded' WHERE id=?", (order["id"],)
        )
        log(db, "relay", order["id"], "superseded",
            "a later original names a different originator; the old direction order is retained")
    next_no = db.execute(
        "SELECT count(*) FROM relay_work_order WHERE case_id=? AND round_id=?",
        (case_id, round_id),
    ).fetchone()[0] + 1
    order_id = f"WO-{case_id}-{round_id}-{next_no}"
    refs, basis = [], []
    for finding in findings:
        if finding["state"] == "proves" and owner in finding["roots"]:
            refs.append({
                "event_id": finding["event_id"],
                "procedure": finding["procedure"],
                "round_id": finding["round_id"],
                "contacts": finding["contacts"],
            })
            basis.extend(finding["reasons"])
    db.execute(
        "INSERT INTO relay_work_order(id,case_id,round_id,owner,state,basis_json,evidence_json,version) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (order_id, case_id, round_id, owner, "open",
         json.dumps(basis, ensure_ascii=False),
         json.dumps(refs, sort_keys=True, ensure_ascii=False), 1),
    )
    log(db, "relay", order_id, "open",
        f"work order assigned to originating endpoint {owner} on signed original evidence")
