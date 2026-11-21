import argparse,json
from pathlib import Path
from .store import connect
from .source import read,file_record
from . import declaration,circuit,meter,safety,report,relay_capture
HANDLERS={'declaration-import':declaration.import_data,'circuit-plan':circuit.import_data,
          'meter-import':meter.import_data,'safety-decide':safety.import_data,'relay-import':relay_capture.import_data}
DEMO=[('declaration-import','declaration_valid.json'),('circuit-plan','circuit_valid.json'),
      ('meter-import','meter_valid.json'),('safety-decide','safety_restore.json')]
def main():
    p=argparse.ArgumentParser(prog='boothpower')
    p.add_argument('command',choices=[*HANDLERS,'demo','report','audit'])
    p.add_argument('--db',default='state.sqlite');p.add_argument('--input')
    a=p.parse_args();db=connect(a.db)
    if a.command=='report':print(report.render(db));return
    if a.command=='audit':print(json.dumps(report.audit(db),ensure_ascii=False,indent=2));return
    if a.command=='demo':
        root=Path(__file__).resolve().parents[1]/'fixtures';steps=[]
        for command,name in DEMO:
            data,raw=read(root/name)
            result=HANDLERS[command](db,data);file_record(db,command,raw)
            steps.append({'command':command,'result':result})
        print(json.dumps({'steps':steps,'audit':report.audit(db),'report':report.build(db)},ensure_ascii=False,indent=2));return
    if not a.input:p.error('--input required')
    data,raw=read(a.input);result=HANDLERS[a.command](db,data);file_record(db,a.command,raw)
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()

