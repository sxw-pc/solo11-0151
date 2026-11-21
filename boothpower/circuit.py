from .source import record
from .store import log

def overlap(a,b,c,d):return a<d and c<b
def current(db,circuit):
    rows=db.execute('SELECT * FROM circuit_version WHERE circuit=? ORDER BY version DESC,event_id',(circuit,)).fetchall()
    if not rows:return None,'circuit version absent'
    top=[r for r in rows if r['version']==rows[0]['version']]
    if len({(r['phase'],r['capacity_kw'],r['start'],r['end']) for r in top})>1:return None,'parallel circuit capacity conflict'
    return top[0],None
def phase_capacity(db,phase):
    capacities=[]
    for row in db.execute('SELECT DISTINCT circuit FROM circuit_version'):
        latest,error=current(db,row['circuit'])
        if error:return None,error
        if latest['phase']==phase:capacities.append(latest['capacity_kw'])
    return (min(capacities),'') if capacities else (None,'phase capacity absent')
def candidate(db,a,exclude_id=None):
    circuit,reason=current(db,a['circuit'])
    if reason:return reason,None,None
    if a['start']<circuit['start'] or a['end']>circuit['end']:return 'outside circuit service window',None,circuit['event_id']
    cap,cap_error=phase_capacity(db,circuit['phase'])
    if cap_error:return cap_error,None,circuit['event_id']
    declaration_version=None
    if a.get('formal',True):
        dec=db.execute('SELECT * FROM declaration_decision WHERE booth=?',(a['booth'],)).fetchone()
        if not dec or dec['state'] in ('unsigned','disputed') or a['kw']!=dec['allowed_kw']:
            return 'declaration capacity unavailable',None,circuit['event_id']
        declaration_version=dec['version']
    from .safety import circuit_isolated
    if circuit_isolated(db,a['circuit']):return 'circuit isolated pending independent reset',declaration_version,circuit['event_id']
    used=0.0
    for row in db.execute("SELECT * FROM power_allocation WHERE state='confirmed' AND id<>?",(exclude_id or a['id'],)):
        other,other_error=current(db,row['circuit'])
        if other_error:return 'existing phase allocation has uncertain circuit version',declaration_version,circuit['event_id']
        if other['phase']==circuit['phase'] and overlap(a['start'],a['end'],row['start'],row['end']):used+=row['kw']
    if used+a['kw']>cap:
        return 'overlapping peak exceeds breaker capacity',declaration_version,circuit['event_id']
    return None,declaration_version,circuit['event_id']
def promote(db):
    waiting=db.execute("SELECT * FROM power_allocation WHERE state='waiting' ORDER BY priority DESC,start,id").fetchall()
    for row in waiting:
        reason,dv,cv=candidate(db,dict(row))
        if reason is None:
            db.execute("UPDATE power_allocation SET state='confirmed',reason='promoted',decl_version=?,circuit_event=? WHERE id=?",(dv,cv,row['id']))
            db.execute('DELETE FROM waiting_application WHERE id=?',(row['id'],))
            log(db,'circuit',row['id'],'confirmed','promoted from waiting')
def reconcile_booth(db,booth):
    dec=db.execute('SELECT * FROM declaration_decision WHERE booth=?',(booth,)).fetchone()
    if not dec:return
    for row in db.execute("SELECT * FROM power_allocation WHERE booth=? AND state='confirmed'",(booth,)).fetchall():
        if row['kw']!=dec['allowed_kw'] or dec['state'] in ('unsigned','disputed'):
            db.execute("UPDATE power_allocation SET state='waiting',reason='signed load changed; new capacity plan required' WHERE id=?",(row['id'],))
            db.execute('INSERT OR REPLACE INTO waiting_application(id,reason,rank) VALUES(?,?,?)',(row['id'],'signed load changed; new capacity plan required',row['priority']))
            log(db,'circuit',row['id'],'waiting','signed load changed; new capacity plan required')
    promote(db)
def reconcile_phase(db,phase):
    affected=[]
    for row in db.execute("SELECT * FROM power_allocation WHERE state='confirmed'").fetchall():
        c,error=current(db,row['circuit'])
        if error or (c and c['phase']==phase):affected.append(row)
    for row in affected:
        db.execute("UPDATE power_allocation SET state='waiting',reason='circuit version requires phase reallocation' WHERE id=?",(row['id'],))
        db.execute('INSERT OR REPLACE INTO waiting_application(id,reason,rank) VALUES(?,?,?)',
                   (row['id'],'circuit version requires phase reallocation',row['priority']))
        log(db,'circuit',row['id'],'waiting','circuit version requires phase reallocation')
    promote(db)
def import_data(db,data):
    out=[]
    with db:
        for c in data.get('circuits',[]):
            if record(db,c['event_id'],'circuit',c):
                db.execute('INSERT INTO circuit_version(event_id,circuit,version,phase,capacity_kw,start,end) VALUES(?,?,?,?,?,?,?)',
                           (c['event_id'],c['circuit'],c['version'],c['phase'],c['capacity_kw'],c['start'],c['end']))
                reconcile_phase(db,c['phase'])
        for a in sorted(data.get('applications',[]),key=lambda x:(-x['priority'],x['start'],x['id'])):
            if not record(db,a['id'],'application',a):
                out.append({'id':a['id'],'state':'idempotent'});continue
            old=db.execute("SELECT id FROM power_allocation WHERE booth=? AND state='confirmed'",(a['booth'],)).fetchone()
            reason,dv,cv=candidate(db,a,old['id'] if old else None)
            if old and reason is None:
                db.execute("UPDATE power_allocation SET state='released',reason='replaced by validated plan' WHERE id=?",(old['id'],))
                log(db,'circuit',old['id'],'released','replaced by validated plan')
            state='confirmed' if reason is None else 'waiting'
            db.execute('INSERT INTO power_allocation(id,booth,circuit,start,end,kw,formal,priority,state,reason,decl_version,circuit_event) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (a['id'],a['booth'],a['circuit'],a['start'],a['end'],a['kw'],int(a.get('formal',True)),a['priority'],state,reason or 'capacity confirmed',dv,cv))
            if reason:db.execute('INSERT INTO waiting_application(id,reason,rank) VALUES(?,?,?)',(a['id'],reason,a['priority']))
            log(db,'circuit',a['id'],state,reason or 'capacity confirmed',a['id'])
            out.append({'id':a['id'],'state':state,'reason':reason})
        promote(db)
        from .safety import reconcile_all
        reconcile_all(db)
    return out
