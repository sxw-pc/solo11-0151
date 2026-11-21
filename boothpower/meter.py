from .source import record
from .store import log

def classify(db,circuit,readings_only=False):
    readings=db.execute('SELECT * FROM meter_reading WHERE circuit=? ORDER BY at,event_id',(circuit,)).fetchall()
    last=None
    for r in readings:
        assignment=db.execute('SELECT * FROM meter_assignment WHERE meter=? AND circuit=? AND start<=? AND end>=? ORDER BY start DESC,event_id',
                              (r['meter'],circuit,r['at'],r['at'])).fetchone()
        if not assignment or assignment['calibrated_to']<r['at']:
            state,reason='untrusted','meter assignment or calibration invalid'
        elif last and r['meter']!=last['meter'] and (not assignment['transfer_witness'] or assignment['transfer_witness']==assignment['operator']):
            state,reason='disputed','meter replacement has no independent handoff'
        elif last and r['meter']==last['meter'] and r['total_kwh']<last['total_kwh'] and (not r['reset_witness'] or r['reset_witness']==assignment['operator']):
            state,reason='disputed','negative cumulative reading without independent rollover witness'
        else:
            state,reason='trusted','assignment calibrated and cumulative reading monotonic'
        if (r['state'],r['reason'])!=(state,reason):
            db.execute('UPDATE meter_reading SET state=?,reason=? WHERE event_id=?',(state,reason,r['event_id']))
            log(db,'meter',r['event_id'],state,reason,r['event_id'])
        last=r
    if readings_only:return None
    from .circuit import current,phase_capacity
    c,_=current(db,circuit)
    if not c: state,reason,peak='pending','circuit capacity not yet known',0
    else:
        cap,_=phase_capacity(db,c['phase'])
        unknown_cap=cap is None
        if unknown_cap:cap=0
        recent=db.execute('SELECT * FROM meter_reading WHERE circuit=? ORDER BY at DESC,event_id DESC LIMIT 3',(circuit,)).fetchall()
        totals=[]
        for r in recent:
            if r['state']!='trusted':totals.append(None);continue
            active={circuit}
            for allocation in db.execute("SELECT DISTINCT circuit FROM power_allocation WHERE state='confirmed' AND start<=? AND end>?",(r['at'],r['at'])):
                other,_=current(db,allocation['circuit'])
                if other and other['phase']==c['phase']:active.add(allocation['circuit'])
            values=[]
            for name in active:
                observed=db.execute("SELECT peak_kw FROM meter_reading WHERE circuit=? AND at=? AND state='trusted' ORDER BY event_id DESC LIMIT 1",(name,r['at'])).fetchone()
                if not observed:break
                values.append(observed['peak_kw'])
            totals.append(sum(values) if len(values)==len(active) else None)
        high=[value for value in totals if value is not None and value>cap]
        peak=max((value for value in totals if value is not None),default=0)
        if unknown_cap:
            state,reason='pending','phase capacity version disputed'
        elif len(recent)==3 and len(high)==3:
            state,reason='confirmed','three consecutive trusted samples over breaker capacity'
        elif high:
            state,reason='observation','short high pulse below confirmation threshold'
        elif any(value is None for value in totals):
            state,reason='pending','phase meters unaligned or disputed; cannot prove overload cleared'
        else: state,reason='clear','recent trusted samples within capacity'
    old=db.execute('SELECT * FROM overload_case WHERE circuit=?',(circuit,)).fetchone()
    version=old['version']+1 if old and (old['state'],old['reason'])!=(state,reason) else (old['version'] if old else 1)
    if not old or (old['state'],old['reason'])!=(state,reason):
        db.execute('INSERT INTO overload_case(circuit,state,start,end,peak_kw,reason,version) VALUES(?,?,?,?,?,?,?) ON CONFLICT(circuit) DO UPDATE SET state=excluded.state,start=excluded.start,end=excluded.end,peak_kw=excluded.peak_kw,reason=excluded.reason,version=excluded.version',
                   (circuit,state,recent[-1]['at'] if c and recent else None,recent[0]['at'] if c and recent else None,peak,reason,version))
        log(db,'meter',circuit,state,reason)
    return {'circuit':circuit,'state':state,'reason':reason,'version':version}

def import_data(db,data):
    with db:
        circuits=set()
        for a in data.get('assignments',[]):
            if record(db,a['event_id'],'meter_assignment',a):
                db.execute('INSERT INTO meter_assignment(event_id,meter,circuit,start,end,calibrated_to,operator,transfer_witness) VALUES(?,?,?,?,?,?,?,?)',
                           (a['event_id'],a['meter'],a['circuit'],a['start'],a['end'],a['calibrated_to'],a.get('operator','meter-operator'),a.get('transfer_witness')))
            circuits.add(a['circuit'])
        for r in data.get('readings',[]):
            if record(db,r['event_id'],'meter_reading',r):
                db.execute('INSERT INTO meter_reading(event_id,meter,circuit,at,total_kwh,peak_kw,reset_witness,state,reason) VALUES(?,?,?,?,?,?,?,?,?)',
                           (r['event_id'],r['meter'],r['circuit'],r['at'],r['total_kwh'],r['peak_kw'],r.get('reset_witness'),'new','not classified'))
            circuits.add(r['circuit'])
        for c in sorted(circuits):classify(db,c,readings_only=True)
        out=[classify(db,c) for c in sorted(circuits)]
        from .safety import reconcile_all
        reconcile_all(db)
    return out
