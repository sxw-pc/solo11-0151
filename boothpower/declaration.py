import json
from .source import record
from .store import log

def current(db,booth):return db.execute('SELECT * FROM declaration_decision WHERE booth=?',(booth,)).fetchone()
def decide(db,booth):
    declarations=db.execute('SELECT * FROM load_declaration WHERE booth=? ORDER BY version DESC,event_id',(booth,)).fetchall()
    if not declarations:return None
    latest=declarations[0];top=[r for r in declarations if r['version']==latest['version']]
    contract=db.execute('SELECT * FROM booth_contract WHERE booth=? AND start<=? AND end>=? ORDER BY start DESC',(booth,latest['at'],latest['at'])).fetchone()
    old=current(db,booth)
    if not contract or contract['signer']!='venue':
        state,reason,allowed,chosen='unsigned','valid venue contract missing',0,None
    elif len({(r['kw'],r['devices_json']) for r in top})>1:
        state,reason,allowed,chosen='disputed','parallel declaration version conflict',old['allowed_kw'] if old else 0,None
    elif old and latest['kw']>old['allowed_kw'] and latest['approved_by']!='electrician':
        state,reason,allowed,chosen='pending_increase','capacity increase awaits electrician signature',old['allowed_kw'],old['event_id']
    else:
        state,reason,allowed,chosen='approved','venue contract and load signed',latest['kw'],latest['event_id']
    if old and (old['state'],old['reason'],old['allowed_kw'],old['event_id'])==(state,reason,allowed,chosen):return dict(old)
    version=old['version']+1 if old else 1
    db.execute('INSERT INTO declaration_decision(booth,state,allowed_kw,reason,event_id,version) VALUES(?,?,?,?,?,?) ON CONFLICT(booth) DO UPDATE SET state=excluded.state,allowed_kw=excluded.allowed_kw,reason=excluded.reason,event_id=excluded.event_id,version=excluded.version',
               (booth,state,allowed,reason,chosen,version))
    log(db,'declaration',booth,state,reason,latest['event_id'])
    from .circuit import reconcile_booth
    reconcile_booth(db,booth)
    from .safety import reconcile_all
    reconcile_all(db)
    return dict(current(db,booth))
def import_data(db,data):
    with db:
        touched=set()
        for c in data.get('contracts',[]):
            if record(db,c['event_id'],'contract',c):
                db.execute('INSERT INTO booth_contract(event_id,booth,start,end,signer) VALUES(?,?,?,?,?)',
                           (c['event_id'],c['booth'],c['start'],c['end'],c['signer']))
            touched.add(c['booth'])
        for d in data.get('declarations',[]):
            if record(db,d['event_id'],'declaration',d):
                db.execute('INSERT INTO load_declaration(event_id,booth,version,kw,at,approved_by,devices_json) VALUES(?,?,?,?,?,?,?)',
                           (d['event_id'],d['booth'],d['version'],d['kw'],d['at'],d.get('approved_by'),json.dumps(d.get('devices',[]))))
            touched.add(d['booth'])
        return [decide(db,b) for b in sorted(touched) if db.execute('SELECT 1 FROM load_declaration WHERE booth=?',(b,)).fetchone()]
