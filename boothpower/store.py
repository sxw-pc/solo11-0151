import sqlite3
SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS evidence(event_id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL,received_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS journal(id INTEGER PRIMARY KEY,command TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS booth_contract(event_id TEXT PRIMARY KEY,booth TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,signer TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS load_declaration(event_id TEXT PRIMARY KEY,booth TEXT NOT NULL,version INTEGER NOT NULL,kw REAL NOT NULL,at TEXT NOT NULL,approved_by TEXT,devices_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS declaration_decision(booth TEXT PRIMARY KEY,state TEXT NOT NULL,allowed_kw REAL NOT NULL,reason TEXT NOT NULL,event_id TEXT,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS circuit_version(event_id TEXT PRIMARY KEY,circuit TEXT NOT NULL,version INTEGER NOT NULL,phase TEXT NOT NULL,capacity_kw REAL NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS power_allocation(id TEXT PRIMARY KEY,booth TEXT NOT NULL,circuit TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,kw REAL NOT NULL,formal INTEGER NOT NULL,priority INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,decl_version INTEGER,circuit_event TEXT);
CREATE TABLE IF NOT EXISTS waiting_application(id TEXT PRIMARY KEY,reason TEXT NOT NULL,rank INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS meter_assignment(event_id TEXT PRIMARY KEY,meter TEXT NOT NULL,circuit TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,calibrated_to TEXT NOT NULL,operator TEXT NOT NULL,transfer_witness TEXT);
CREATE TABLE IF NOT EXISTS meter_reading(event_id TEXT PRIMARY KEY,meter TEXT NOT NULL,circuit TEXT NOT NULL,at TEXT NOT NULL,total_kwh REAL NOT NULL,peak_kw REAL NOT NULL,reset_witness TEXT,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS overload_case(circuit TEXT PRIMARY KEY,state TEXT NOT NULL,start TEXT,end TEXT,peak_kw REAL NOT NULL,reason TEXT NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS breaker_trip(event_id TEXT PRIMARY KEY,circuit TEXT NOT NULL,booth TEXT NOT NULL,role TEXT NOT NULL,actor TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS safety_inspection(event_id TEXT PRIMARY KEY,circuit TEXT NOT NULL,remediator TEXT NOT NULL,approver TEXT NOT NULL,at TEXT NOT NULL,cooled_minutes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS energize_permit(booth TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,contract_version INTEGER,allocation_id TEXT,meter_version INTEGER,trip_version INTEGER);
CREATE TABLE IF NOT EXISTS bench_round(meter TEXT PRIMARY KEY,seal_round INTEGER NOT NULL,opened_at TEXT);
CREATE TABLE IF NOT EXISTS bench_evidence(event_id TEXT PRIMARY KEY,meter TEXT NOT NULL,round_id INTEGER NOT NULL,station TEXT NOT NULL,true_load INTEGER NOT NULL,display INTEGER NOT NULL,excitation_id TEXT);
CREATE TABLE IF NOT EXISTS field_reading(event_id TEXT PRIMARY KEY,meter TEXT NOT NULL,circuit TEXT NOT NULL,excitation_id TEXT NOT NULL,true_load INTEGER NOT NULL,display INTEGER NOT NULL,reference_bias REAL);
CREATE TABLE IF NOT EXISTS field_excitation(id TEXT PRIMARY KEY,meter TEXT NOT NULL,circuit TEXT NOT NULL,load_before INTEGER NOT NULL,load_after INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS bench_certificate(round_id TEXT PRIMARY KEY,meter TEXT,state TEXT NOT NULL,tolerance REAL NOT NULL,corrections_json TEXT NOT NULL,counterexamples_json TEXT NOT NULL,segments_json TEXT NOT NULL,curves_json TEXT NOT NULL,certificate_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision_log(id INTEGER PRIMARY KEY,subsystem TEXT NOT NULL,object_id TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,evidence_id TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""
def connect(path):
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
def log(db,subsystem,object_id,state,reason,evidence_id=None):
    db.execute('INSERT INTO decision_log(subsystem,object_id,state,reason,evidence_id) VALUES(?,?,?,?,?)',
               (subsystem,object_id,state,reason,evidence_id))
