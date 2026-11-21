"""Fixed-round evidence intake for bench and field re-verification.

Evidence must stay inside one verification round: after the meter is
re-sealed (``seal_round`` advances) originals from the old round cannot
continue to support the current round. Bench work happens off the live
circuit with independently chosen standard loads; field joint readings bind
two raw sequences and one shared excitation, and the reference meter's own
bias is never treated as absolute truth.
"""

from .source import record
from .store import log

BENCH_STATION = "bench"
FIELD_STATION = "field"


def current_round(db, meter):
    row = db.execute(
        "SELECT seal_round FROM bench_round WHERE meter=?", (meter,)
    ).fetchone()
    return row["seal_round"] if row else 0


def open_round(db, meter, seal_round=None):
    """Open a round; re-sealing always advances and never moves backwards.

    An explicit ``seal_round`` may only move forward: re-opening an older
    seal would let stale originals appear to belong to the current round.
    """
    previous = current_round(db, meter)
    if seal_round is None:
        seal_round = previous + 1
    elif seal_round < previous:
        raise ValueError(
            f"cannot re-open seal round {seal_round}; meter {meter} is already on round {previous}"
        )
    db.execute(
        "INSERT INTO bench_round(meter,seal_round,opened_at) VALUES(?,?,CURRENT_TIMESTAMP) "
        "ON CONFLICT(meter) DO UPDATE SET seal_round=excluded.seal_round",
        (meter, seal_round),
    )
    if seal_round != previous:
        log(db, "bench-round", meter, "opened", f"seal round {seal_round}")
    return seal_round


def _require_round(db, meter, round_id):
    active = current_round(db, meter)
    if round_id != active:
        raise ValueError(
            f"evidence round {round_id} is not the active round {active} for {meter}; "
            "originals from a previous seal cannot support this round"
        )


def _store(db, event_id, meter, round_id, station, load, display, excitation):
    record(db, event_id, "bench_evidence", {
        "meter": meter,
        "round": round_id,
        "station": station,
        "true_load": load,
        "display": display,
        "excitation": excitation,
    })
    db.execute(
        "INSERT INTO bench_evidence(event_id,meter,round_id,station,true_load,display,"
        "excitation_id) VALUES(?,?,?,?,?,?,?)",
        (event_id, meter, round_id, station, load, display, excitation),
    )
    log(db, "bench-evidence", meter, station, f"load {load} -> display {display}", event_id)


def add_bench_observation(db, meter, event_id, load, display, round_id=None):
    """One bench standard-load response captured off the live circuit."""
    round_id = current_round(db, meter) if round_id is None else round_id
    _require_round(db, meter, round_id)
    _store(db, event_id, meter, round_id, BENCH_STATION, load, display, f"bench:{event_id}")
    return round_id


def add_field_set(db, meter, field_set):
    """Validate and bind one two-meter field joint reading.

    Both raw sequences must see the same circuit and the same shared
    excitation; the known load change on the excitation is the only truth
    anchor. The reference reading is stored for traceability but never used as
    an absolute load value.
    """
    readings = field_set["readings"]
    meters = {item["meter"] for item in readings}
    if len(readings) != 2 or len(meters) != 2:
        raise ValueError("field joint reading requires exactly two meters and two raw sequences")
    if meter not in meters:
        raise ValueError(f"target meter {meter} is not part of the field set")
    excitation = field_set["excitation"]
    circuit = excitation.get("circuit")
    if any(item.get("circuit") != circuit for item in readings):
        raise ValueError("field sequences do not observe the same circuit")
    excitation_id = excitation["id"]
    if any(item.get("excitation_id") != excitation_id for item in readings):
        raise ValueError("field sequences are not bound to one shared excitation")
    load_before = excitation["load_before"]
    load_after = excitation["load_after"]
    if load_after == load_before:
        raise ValueError("field excitation has no known load change; cannot bind a joint reading")
    target = next(item for item in readings if item["meter"] == meter)
    round_id = field_set.get("round_id", current_round(db, meter))
    _require_round(db, meter, round_id)
    for item in readings:
        record(db, item["event_id"], "field_reading", item)
        db.execute(
            "INSERT INTO field_reading(event_id,meter,circuit,excitation_id,"
            "true_load,display,reference_bias) VALUES(?,?,?,?,?,?,?)",
            (
                item["event_id"],
                item["meter"],
                item["circuit"],
                excitation_id,
                item["true_load"],
                item["display"],
                item.get("reference_bias"),
            ),
        )
    record(db, excitation_id, "field_excitation", excitation)
    db.execute(
        "INSERT INTO field_excitation(id,meter,circuit,load_before,load_after) "
        "VALUES(?,?,?,?,?)",
        (excitation_id, meter, circuit, load_before, load_after),
    )
    _store(
        db,
        f"field:{excitation_id}:{meter}",
        meter,
        round_id,
        FIELD_STATION,
        target["true_load"],
        target["display"],
        excitation_id,
    )
    return round_id


def round_observations(db, round_id, station=None):
    sql = "SELECT * FROM bench_evidence WHERE round_id=?"
    params = [round_id]
    if station is not None:
        sql += " AND station=?"
        params.append(station)
    sql += " ORDER BY rowid"
    return [
        {"event_id": row["event_id"], "meter": row["meter"], "station": row["station"],
         "true_load": row["true_load"], "display": row["display"],
         "excitation_id": row["excitation_id"]}
        for row in db.execute(sql, params).fetchall()
    ]
