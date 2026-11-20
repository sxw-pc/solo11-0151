import copy,json,subprocess,sys,tempfile,unittest
from pathlib import Path
import boothpower
from boothpower.store import connect
from boothpower import declaration,circuit,meter,safety,report
ROOT=Path(__file__).resolve().parents[1]
def fixture(name):return json.loads((ROOT/'fixtures'/name).read_text(encoding='utf8'))
def get(db,table,key,value):return db.execute(f'SELECT * FROM {table} WHERE {key}=?',(value,)).fetchone()
class BoothPowerTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=connect(str(Path(self.tmp.name)/'state.sqlite'))
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def ready(self):
        declaration.import_data(self.db,fixture('declaration_valid.json'))
        circuit.import_data(self.db,fixture('circuit_valid.json'))
        meter.import_data(self.db,fixture('meter_valid.json'))
    def test_module_origin(self):
        print(boothpower.__file__)
        self.assertTrue(str(Path(boothpower.__file__).resolve()).startswith(str(ROOT)))
    def test_contract_independent(self):
        d=fixture('declaration_valid.json');d['contracts']=[]
        self.assertEqual(declaration.import_data(self.db,d)[0]['state'],'unsigned')
    def test_four_ledgers_independent(self):
        self.assertEqual(declaration.import_data(self.db,fixture('declaration_valid.json'))[0]['state'],'approved')
        self.assertEqual(circuit.import_data(self.db,fixture('circuit_valid.json'))[0]['state'],'confirmed')
        self.assertEqual(meter.import_data(self.db,fixture('meter_valid.json'))[0]['state'],'clear')
        self.assertEqual(safety.import_data(self.db,fixture('safety_restore.json'))[0]['state'],'energized')
    def test_late_unsigned_increase_retains_old_limit(self):
        self.ready()
        self.assertEqual(declaration.import_data(self.db,fixture('declaration_changed.json'))[0]['state'],'pending_increase')
        self.assertEqual(get(self.db,'declaration_decision','booth','A')['allowed_kw'],3)
        self.assertEqual(get(self.db,'power_allocation','id','alloc-A1')['state'],'confirmed')
    def test_signed_increase_invalidates_power_permission(self):
        self.ready()
        data=fixture('declaration_changed.json');data['declarations'][0]['approved_by']='electrician'
        declaration.import_data(self.db,data)
        self.assertEqual(get(self.db,'power_allocation','id','alloc-A1')['state'],'waiting')
        self.assertEqual(get(self.db,'energize_permit','booth','A')['state'],'blocked')
    def test_overlapping_peak_has_waiting_application(self):
        out=circuit.import_data(self.db,fixture('circuit_overbook.json'))
        self.assertEqual([x['state'] for x in out],['confirmed','waiting'])
        self.assertIsNotNone(get(self.db,'waiting_application','id','alloc-Y1'))
    def test_different_circuits_share_phase_breaker(self):
        data=fixture('circuit_overbook.json')
        extra=dict(data['circuits'][0],event_id='circuit-C3-v1',circuit='C3',phase='L2')
        data['circuits'].append(extra)
        data['applications'][1]['circuit']='C3'
        out=circuit.import_data(self.db,data)
        self.assertEqual(out[1]['state'],'waiting')
        self.assertEqual(out[1]['reason'],'overlapping peak exceeds breaker capacity')
    def test_different_nominal_limits_use_conservative_phase_cap(self):
        data=fixture('circuit_overbook.json')
        data['circuits'][0]['capacity_kw']=4
        data['applications'][0]['kw']=3
        second=dict(data['circuits'][0],event_id='circuit-C3-v1',circuit='C3',capacity_kw=8)
        data['circuits'].append(second)
        data['applications'][1].update(circuit='C3',kw=3)
        out=circuit.import_data(self.db,data)
        self.assertEqual([x['state'] for x in out],['confirmed','waiting'])
    def test_aligned_cross_circuit_peaks_confirm_phase_overload(self):
        plan=fixture('circuit_overbook.json')
        plan['circuits'][0]['capacity_kw']=5
        plan['applications'][0]['kw']=2
        plan['applications'][1].update(circuit='C3',kw=2)
        plan['circuits'].append(dict(plan['circuits'][0],event_id='circuit-C3-v1',circuit='C3',capacity_kw=8))
        self.assertEqual([x['state'] for x in circuit.import_data(self.db,plan)],['confirmed','confirmed'])
        assignments=[];readings=[]
        for name,circuit_id in [('M2','C2'),('M3','C3')]:
            assignments.append({'event_id':'assign-'+name,'meter':name,'circuit':circuit_id,'start':'2026-11-17T08:00:00','end':'2026-11-17T18:00:00','calibrated_to':'2026-12-01T00:00:00'})
            for i in range(3):
                readings.append({'event_id':f'{name}-peak-{i}','meter':name,'circuit':circuit_id,'at':f'2026-11-17T09:{10+i*10:02d}:00','total_kwh':100+i,'peak_kw':3})
        out=meter.import_data(self.db,{'assignments':assignments,'readings':readings})
        self.assertEqual({x['circuit']:x['state'] for x in out},{'C2':'confirmed','C3':'confirmed'})
    def test_late_circuit_capacity_reallocates_confirmed_slots(self):
        declaration.import_data(self.db,fixture('declaration_valid.json'))
        circuit.import_data(self.db,fixture('circuit_valid.json'))
        revised=dict(fixture('circuit_valid.json')['circuits'][0],event_id='circuit-C1-v2',version=2,capacity_kw=2)
        circuit.import_data(self.db,{'circuits':[revised]})
        self.assertEqual(get(self.db,'power_allocation','id','alloc-A1')['state'],'waiting')
        self.assertEqual(get(self.db,'power_allocation','id','alloc-B1')['state'],'waiting')
    def test_failed_replan_keeps_old_confirmed(self):
        declaration.import_data(self.db,fixture('declaration_valid.json'))
        circuit.import_data(self.db,fixture('circuit_valid.json'))
        a=fixture('circuit_valid.json')['applications'][0].copy()
        a.update(id='bad-replan',kw=9)
        self.assertEqual(circuit.import_data(self.db,{'applications':[a]})[0]['state'],'waiting')
        self.assertEqual(get(self.db,'power_allocation','id','alloc-A1')['state'],'confirmed')
    def test_meter_expiry_untrusted(self):
        circuit.import_data(self.db,fixture('circuit_valid.json'))
        data=fixture('meter_valid.json');data['assignments'][0]['calibrated_to']='2026-11-17T09:00:00'
        meter.import_data(self.db,data)
        self.assertEqual(get(self.db,'meter_reading','event_id','read-1')['state'],'untrusted')
    def test_negative_cumulative_without_handoff_disputed(self):
        circuit.import_data(self.db,fixture('circuit_valid.json'))
        data=fixture('meter_valid.json');data['readings'][1]['total_kwh']=90
        meter.import_data(self.db,data)
        self.assertEqual(get(self.db,'meter_reading','event_id','read-2')['state'],'disputed')
        self.assertNotEqual(get(self.db,'overload_case','circuit','C1')['state'],'clear')
    def test_witnessed_meter_transfer_accepts_new_baseline(self):
        circuit.import_data(self.db,fixture('circuit_valid.json'))
        meter.import_data(self.db,fixture('meter_valid.json'))
        self.assertEqual(get(self.db,'meter_reading','event_id','read-3')['state'],'trusted')
        self.assertEqual(get(self.db,'meter_reading','event_id','read-3')['meter'],'M3')
    def test_witnessed_rollover_accepts_negative_delta(self):
        circuit.import_data(self.db,fixture('circuit_valid.json'))
        data=fixture('meter_valid.json');data['readings'][1]['total_kwh']=1
        data['readings'][1]['reset_witness']='independent-panel-operator'
        meter.import_data(self.db,data)
        self.assertEqual(get(self.db,'meter_reading','event_id','read-2')['state'],'trusted')
    def test_self_signed_rollover_is_disputed(self):
        circuit.import_data(self.db,fixture('circuit_valid.json'))
        data=fixture('meter_valid.json');data['readings'][1]['total_kwh']=1
        data['readings'][1]['reset_witness']='meter-operator'
        meter.import_data(self.db,data)
        self.assertEqual(get(self.db,'meter_reading','event_id','read-2')['state'],'disputed')
    def test_one_high_pulse_does_not_confirm_overload(self):
        circuit.import_data(self.db,fixture('circuit_overbook.json'))
        data=fixture('meter_overload.json');data['readings']=data['readings'][:1]
        self.assertEqual(meter.import_data(self.db,data)[0]['state'],'observation')
    def test_three_high_samples_confirm_overload(self):
        circuit.import_data(self.db,fixture('circuit_overbook.json'))
        self.assertEqual(meter.import_data(self.db,fixture('meter_overload.json'))[0]['state'],'confirmed')
        self.assertTrue(safety.circuit_isolated(self.db,'C2'))
    def test_overload_reset_promotes_waiter_only_after_independent_clearance(self):
        self.ready()
        high={'readings':[{'event_id':f'late-high-{i}','meter':'M3','circuit':'C1','at':f'2026-11-17T10:{40+i*10:02d}:00','total_kwh':2+i,'peak_kw':6} for i in range(3)]}
        self.assertEqual(meter.import_data(self.db,high)[0]['state'],'confirmed')
        self.assertEqual(get(self.db,'energize_permit','booth','A')['state'],'blocked')
        trips={'trips':[{'event_id':'late-trip-panel','circuit':'C1','booth':'A','role':'panel','actor':'panel-operator','at':'2026-11-17T11:05:00'},
                        {'event_id':'late-trip-site','circuit':'C1','booth':'A','role':'onsite','actor':'safety-warden','at':'2026-11-17T11:05:00'}]}
        safety.import_data(self.db,trips)
        waiting={'applications':[{'id':'wait-C','booth':'C','circuit':'C1','start':'2026-11-17T11:10:00','end':'2026-11-17T11:50:00','kw':2,'priority':1,'formal':False}]}
        self.assertEqual(circuit.import_data(self.db,waiting)[0]['state'],'waiting')
        normal={'readings':[{'event_id':f'normal-after-{i}','meter':'M3','circuit':'C1','at':f'2026-11-17T11:{20+i*10:02d}:00','total_kwh':5+i,'peak_kw':3} for i in range(3)]}
        self.assertEqual(meter.import_data(self.db,normal)[0]['state'],'clear')
        self.assertEqual(get(self.db,'power_allocation','id','wait-C')['state'],'waiting')
        inspection={'inspections':[{'event_id':'late-inspection','circuit':'C1','remediator':'repair-tech','approver':'safety-chief','at':'2026-11-17T11:50:00','cooled_minutes':45}]}
        safety.import_data(self.db,inspection)
        self.assertEqual(get(self.db,'power_allocation','id','wait-C')['state'],'confirmed')
    def test_self_report_trip_not_independent(self):
        self.ready()
        safety.import_data(self.db,fixture('safety_unverified.json'))
        self.assertEqual(safety.trip_state(self.db,'C1')[0],'unverified')
        self.assertEqual(get(self.db,'energize_permit','booth','A')['state'],'energized')
    def test_independent_trip_without_reset_isolates(self):
        self.ready()
        data=fixture('safety_restore.json');data['inspections']=[]
        safety.import_data(self.db,data)
        self.assertTrue(safety.circuit_isolated(self.db,'C1'))
        self.assertEqual(get(self.db,'energize_permit','booth','A')['state'],'isolated')
    def test_reset_requires_role_separation_and_samples(self):
        self.ready()
        data=fixture('safety_restore.json');data['inspections'][0]['approver']='repair-tech'
        safety.import_data(self.db,data)
        self.assertEqual(get(self.db,'energize_permit','booth','A')['state'],'isolated')
    def test_independent_reset_restores_permission(self):
        self.ready()
        safety.import_data(self.db,fixture('safety_restore.json'))
        self.assertEqual(get(self.db,'energize_permit','booth','A')['state'],'energized')
    def test_new_trip_after_reset_revokes_old_permission(self):
        self.ready();safety.import_data(self.db,fixture('safety_restore.json'))
        later={'trips':[{'event_id':'trip-again-panel','circuit':'C1','booth':'A','role':'panel','actor':'panel-operator','at':'2026-11-17T10:50:00'},
                        {'event_id':'trip-again-site','circuit':'C1','booth':'A','role':'onsite','actor':'safety-warden','at':'2026-11-17T10:50:00'}]}
        safety.import_data(self.db,later)
        self.assertEqual(get(self.db,'energize_permit','booth','A')['state'],'isolated')
    def test_event_replay_conflict_and_rollback(self):
        base=fixture('declaration_valid.json');declaration.import_data(self.db,base)
        self.assertEqual(declaration.import_data(self.db,base)[0]['state'],'approved')
        mixed={'declarations':[dict(base['declarations'][0],event_id='new-load',booth='X'),dict(base['declarations'][0],kw=7)]}
        with self.assertRaises(ValueError):declaration.import_data(self.db,mixed)
        self.assertIsNone(get(self.db,'load_declaration','event_id','new-load'))
    def test_cli_raw_replay(self):
        path=str(Path(self.tmp.name)/'cli.sqlite')
        run=subprocess.run([sys.executable,'-m','boothpower','demo','--db',path],cwd=ROOT,capture_output=True,text=True,check=True)
        result=json.loads(run.stdout)
        self.assertTrue(result['audit']['replay_ok'])
        self.assertEqual(result['audit']['journal_count'],4)
        self.assertEqual(result['report']['energize_permit'][0]['state'],'energized')
    def test_report_has_all_ledgers(self):
        self.ready();safety.import_data(self.db,fixture('safety_restore.json'))
        data=report.build(self.db)
        self.assertEqual(len(data['declaration_decision']),2)
        self.assertEqual(len(data['power_allocation']),2)
        self.assertEqual(len(data['meter_reading']),3)
        self.assertEqual(len(data['energize_permit']),2)
if __name__=='__main__':unittest.main()
