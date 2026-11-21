from datetime import datetime
from .source import record
from .store import log

def trip_state(db,circuit):
    rows=db.execute('SELECT * FROM breaker_trip WHERE circuit=? ORDER BY at,event_id',(circuit,)).fetchall()
    if rows:
        latest_at=rows[-1]['at']
        rows=[r for r in rows if r['at']==latest_at]
    panel=next((r for r in rows if r['role']=='panel'),None)
    site=next((r for r in rows if r['role']=='onsite' and (not panel or r['actor']!=panel['actor'])),None)
    if not rows:return 'none',None,'no trip witnesses'
    if not panel or not site:return 'unverified',None,'independent panel and onsite witness required'
    return 'confirmed',max(panel['at'],site['at']),'independent trip witnesses confirm isolation'

def reset_valid(db,circuit,trip_at):
    if not trip_at:return False,'trip not independently witnessed'
    inspections=db.execute('SELECT * FROM safety_inspection WHERE circuit=? ORDER BY at DESC,event_id DESC',(circuit,)).fetchall()
    if not inspections:return False,'independent reset inspection missing'
    i=inspections[0]
    elapsed=(datetime.fromisoformat(i['at'])-datetime.fromisoformat(trip_at)).total_seconds()/60
    if i['remediator']==i['approver'] or i['cooled_minutes']<30 or elapsed<i['cooled_minutes']:
        return False,'reset role separation or cooling interval failed'
    capacity=db.execute('SELECT capacity_kw FROM circuit_version WHERE circuit=? ORDER BY version DESC LIMIT 1',(circuit,)).fetchone()
    if not capacity:return False,'circuit capacity missing'
    samples=db.execute("SELECT * FROM meter_reading WHERE circuit=? AND at>? AND at<=? ORDER BY at DESC LIMIT 3",(circuit,trip_at,i['at'])).fetchall()
    if len(samples)<3 or any(s['state']!='trusted' or s['peak_kw']>capacity['capacity_kw'] for s in samples):
        return False,'three trusted normal post-trip samples required'
    return True,'independent inspection, cooling and normal samples accepted'

def circuit_isolated(db,circuit):
    overload=db.execute('SELECT state FROM overload_case WHERE circuit=?',(circuit,)).fetchone()
    if overload and overload['state'] in ('confirmed','pending'):return True
    state,trip_at,_=trip_state(db,circuit)
    if state=='confirmed':
        approved,_=reset_valid(db,circuit,trip_at)
        return not approved
    return False

def reconcile_all(db):
    booths={r['booth'] for r in db.execute('SELECT booth FROM declaration_decision')}
    booths|={r['booth'] for r in db.execute('SELECT booth FROM power_allocation')}
    out=[]
    for booth in sorted(booths):
        dec=db.execute('SELECT * FROM declaration_decision WHERE booth=?',(booth,)).fetchone()
        allocation=db.execute("SELECT * FROM power_allocation WHERE booth=? AND state='confirmed' ORDER BY start DESC LIMIT 1",(booth,)).fetchone()
        meter=None;trips=0
        if not dec or dec['state'] in ('unsigned','disputed') or dec['allowed_kw']<=0:
            state,reason='blocked','signed load contract unavailable'
        elif not allocation:
            state,reason='blocked','confirmed circuit allocation absent'
        else:
            meter=db.execute('SELECT * FROM overload_case WHERE circuit=?',(allocation['circuit'],)).fetchone()
            trips=db.execute('SELECT count(*) FROM breaker_trip WHERE circuit=?',(allocation['circuit'],)).fetchone()[0]
            if not meter or meter['state'] in ('pending','confirmed'):
                state,reason='blocked','trusted meter clearance absent or overload confirmed'
            elif circuit_isolated(db,allocation['circuit']):
                state,reason='isolated','independently witnessed trip awaits reset'
            elif allocation['kw']>dec['allowed_kw']:
                state,reason='blocked','allocation exceeds signed load'
            else:state,reason='energized','contract allocation meter and safety all valid'
        old=db.execute('SELECT state,reason,contract_version,allocation_id,meter_version,trip_version FROM energize_permit WHERE booth=?',(booth,)).fetchone()
        current_row=(state,reason,dec['version'] if dec else None,allocation['id'] if allocation else None,meter['version'] if meter else None,trips)
        if not old or (old['state'],old['reason'],old['contract_version'],old['allocation_id'],old['meter_version'],old['trip_version'])!=current_row:
            db.execute('INSERT INTO energize_permit(booth,state,reason,contract_version,allocation_id,meter_version,trip_version) VALUES(?,?,?,?,?,?,?) ON CONFLICT(booth) DO UPDATE SET state=excluded.state,reason=excluded.reason,contract_version=excluded.contract_version,allocation_id=excluded.allocation_id,meter_version=excluded.meter_version,trip_version=excluded.trip_version',
                       (booth,*current_row))
            log(db,'safety',booth,state,reason)
        out.append({'booth':booth,'state':state,'reason':reason})
    return out

def import_data(db,data):
    with db:
        for t in data.get('trips',[]):
            if record(db,t['event_id'],'trip',t):
                db.execute('INSERT INTO breaker_trip(event_id,circuit,booth,role,actor,at) VALUES(?,?,?,?,?,?)',
                           (t['event_id'],t['circuit'],t['booth'],t['role'],t['actor'],t['at']))
        for i in data.get('inspections',[]):
            if record(db,i['event_id'],'inspection',i):
                db.execute('INSERT INTO safety_inspection(event_id,circuit,remediator,approver,at,cooled_minutes) VALUES(?,?,?,?,?,?)',
                           (i['event_id'],i['circuit'],i['remediator'],i['approver'],i['at'],i['cooled_minutes']))
        out=reconcile_all(db)
        from .circuit import promote
        for c in {x['circuit'] for x in data.get('trips',[])+data.get('inspections',[])}:
            if not circuit_isolated(db,c):promote(db)
        out=reconcile_all(db)
    return out
