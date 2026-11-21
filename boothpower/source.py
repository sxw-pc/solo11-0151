import hashlib,json
from pathlib import Path
def read(path):
    raw=Path(path).read_bytes();return json.loads(raw),raw
def record(db,event_id,kind,item):
    raw=json.dumps(item,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf8')
    sha=hashlib.sha256(raw).hexdigest()
    old=db.execute('SELECT sha FROM evidence WHERE event_id=?',(event_id,)).fetchone()
    if old:
        if old['sha']!=sha:raise ValueError(f'event conflict: {event_id}')
        return False
    db.execute('INSERT INTO evidence(event_id,kind,sha,raw) VALUES(?,?,?,?)',(event_id,kind,sha,raw))
    return True
def file_record(db,command,raw):
    sha=hashlib.sha256(raw).hexdigest()
    with db:
        db.execute('INSERT OR IGNORE INTO evidence(event_id,kind,sha,raw) VALUES(?,?,?,?)',('file:'+sha,'source-file',sha,raw))
        db.execute('INSERT INTO journal(command,sha,raw) VALUES(?,?,?)',(command,sha,raw))
