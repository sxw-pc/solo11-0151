import hashlib,json
from .store import connect
TABLES=['evidence','declaration_decision','power_allocation','waiting_application','meter_reading','overload_case','breaker_trip','safety_inspection','energize_permit','decision_log']
def build(db):
    out={t:[dict(r) for r in db.execute(f'SELECT * FROM {t} ORDER BY 1')] for t in TABLES}
    for e in out['evidence']:e['raw']=e['raw'].decode('utf8')
    return out
def audit(db):
    from . import declaration,circuit,meter,safety
    handlers={'declaration-import':declaration.import_data,'circuit-plan':circuit.import_data,
              'meter-import':meter.import_data,'safety-decide':safety.import_data}
    rows=db.execute('SELECT * FROM journal ORDER BY id').fetchall()
    if not rows:return {'journal_count':0,'replay_ok':False,'reason':'no CLI source files'}
    fresh=connect(':memory:')
    try:
        for r in rows:
            if hashlib.sha256(r['raw']).hexdigest()!=r['sha']:
                return {'journal_count':len(rows),'replay_ok':False,'reason':'raw source hash mismatch'}
            handlers[r['command']](fresh,json.loads(r['raw']))
        compare=['declaration_decision','power_allocation','waiting_application','meter_reading','overload_case','energize_permit']
        mismatch=[]
        for t in compare:
            a=[tuple(r) for r in db.execute(f'SELECT * FROM {t} ORDER BY 1')]
            b=[tuple(r) for r in fresh.execute(f'SELECT * FROM {t} ORDER BY 1')]
            if a!=b:mismatch.append(t)
        return {'journal_count':len(rows),'replay_ok':not mismatch,'mismatches':mismatch}
    finally:fresh.close()
def render(db):return json.dumps(build(db),ensure_ascii=False,indent=2)
