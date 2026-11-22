import sqlite3

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS evidence(event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, sha TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS file_journal(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, sha TEXT NOT NULL, raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS animal(id TEXT PRIMARY KEY, owner TEXT NOT NULL, arrival TEXT NOT NULL, departure TEXT NOT NULL, daily_fee INTEGER NOT NULL, service_fee INTEGER NOT NULL, requested_medicine TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS health_certificate(event_id TEXT PRIMARY KEY, animal_id TEXT NOT NULL, version INTEGER NOT NULL, chip TEXT NOT NULL, signed_by TEXT NOT NULL, vaccine_until TEXT NOT NULL, contraindications TEXT NOT NULL, condition TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS certificate_revocation(event_id TEXT PRIMARY KEY, certificate_id TEXT NOT NULL, animal_id TEXT NOT NULL, at TEXT NOT NULL, signed_by TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intake_decision(animal_id TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT NOT NULL, certificate_id TEXT, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS care_plan(event_id TEXT PRIMARY KEY, animal_id TEXT NOT NULL, version INTEGER NOT NULL, food TEXT NOT NULL, medicine TEXT NOT NULL, meals INTEGER NOT NULL, approved_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS care_action(event_id TEXT PRIMARY KEY, animal_id TEXT NOT NULL, plan_id TEXT NOT NULL, kind TEXT NOT NULL, day TEXT NOT NULL, actor TEXT NOT NULL, witness TEXT, food TEXT, medicine TEXT, supersedes TEXT);
CREATE TABLE IF NOT EXISTS care_decision(animal_id TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT NOT NULL, missed_meals INTEGER NOT NULL, compensation INTEGER NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS pickup_authority(event_id TEXT PRIMARY KEY, animal_id TEXT NOT NULL, person TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT NOT NULL, signed_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS handoff(event_id TEXT PRIMARY KEY, animal_id TEXT NOT NULL, at TEXT NOT NULL, person TEXT NOT NULL, witness TEXT, condition TEXT NOT NULL, veterinarian TEXT, dispute TEXT);
CREATE TABLE IF NOT EXISTS injury_case(animal_id TEXT PRIMARY KEY, intake_condition TEXT NOT NULL, return_condition TEXT NOT NULL, evidence_id TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS injury_resolution(event_id TEXT PRIMARY KEY, animal_id TEXT NOT NULL, handoff_id TEXT NOT NULL, veterinarian TEXT NOT NULL, owner TEXT NOT NULL, decision TEXT NOT NULL, rationale TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fee_line(animal_id TEXT NOT NULL, kind TEXT NOT NULL, amount INTEGER NOT NULL, basis TEXT NOT NULL, frozen INTEGER NOT NULL, PRIMARY KEY(animal_id,kind));
CREATE TABLE IF NOT EXISTS handoff_decision(animal_id TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT NOT NULL, overdue_fee INTEGER NOT NULL, frozen_fee INTEGER NOT NULL, payable_fee INTEGER NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS decision_history(id INTEGER PRIMARY KEY, subsystem TEXT NOT NULL, animal_id TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL, evidence_id TEXT, at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS medical_claim(event_id TEXT PRIMARY KEY, claim_id TEXT NOT NULL UNIQUE, animal_id TEXT NOT NULL, chip TEXT NOT NULL, hospital TEXT NOT NULL, visit_at TEXT NOT NULL, handoff_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS medical_policy(event_id TEXT PRIMARY KEY, claim_id TEXT NOT NULL UNIQUE, animal_id TEXT NOT NULL, chip TEXT NOT NULL, insurer TEXT NOT NULL, policy_no TEXT NOT NULL, coverage_from TEXT NOT NULL, coverage_to TEXT NOT NULL, deductible INTEGER NOT NULL, coverage_limit INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS hospital_bill(event_id TEXT PRIMARY KEY, claim_id TEXT NOT NULL, chip TEXT NOT NULL, hospital TEXT NOT NULL, visit_at TEXT NOT NULL, line_no TEXT NOT NULL, description TEXT NOT NULL, amount INTEGER NOT NULL, UNIQUE(claim_id,line_no));
CREATE TABLE IF NOT EXISTS insurance_approval(event_id TEXT PRIMARY KEY, claim_id TEXT NOT NULL, at TEXT NOT NULL, reviewer TEXT NOT NULL, decision TEXT NOT NULL, approved_amount INTEGER NOT NULL, note TEXT);
CREATE TABLE IF NOT EXISTS bank_transfer(event_id TEXT PRIMARY KEY, claim_id TEXT NOT NULL, transaction_id TEXT NOT NULL UNIQUE, payer TEXT NOT NULL, payee TEXT NOT NULL, amount INTEGER NOT NULL, at TEXT NOT NULL, status TEXT NOT NULL, reverses TEXT, memo TEXT);
CREATE TABLE IF NOT EXISTS medical_claim_decision(claim_id TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT NOT NULL, bill_total INTEGER NOT NULL, covered_medical INTEGER NOT NULL, deductible INTEGER NOT NULL, insurance_payable INTEGER NOT NULL, insurance_paid INTEGER NOT NULL, insurer_paid_hospital INTEGER NOT NULL, insurer_paid_owner INTEGER NOT NULL, owner_paid_hospital INTEGER NOT NULL, owner_reimbursed INTEGER NOT NULL, owner_out_of_pocket INTEGER NOT NULL, hospital_received INTEGER NOT NULL, hospital_outstanding INTEGER NOT NULL, insurance_outstanding INTEGER NOT NULL, owner_pending_reimbursement INTEGER NOT NULL, recovery_due INTEGER NOT NULL, settlement_route TEXT NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS recovery_item(id INTEGER PRIMARY KEY, claim_id TEXT NOT NULL, party TEXT NOT NULL, amount INTEGER NOT NULL, reason TEXT NOT NULL, basis TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claim_decision_history(id INTEGER PRIMARY KEY, claim_id TEXT NOT NULL, animal_id TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL, evidence_id TEXT, at TEXT DEFAULT CURRENT_TIMESTAMP);
"""


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def update_decision(db, table, animal_id, fields, evidence_id=None):
    old = db.execute(f"SELECT * FROM {table} WHERE animal_id=?", (animal_id,)).fetchone()
    names = list(fields)
    if old and all(old[name] == fields[name] for name in names):
        return dict(old)
    version = old['version'] + 1 if old else 1
    columns = ['animal_id', *names, 'version']
    values = [animal_id, *(fields[name] for name in names), version]
    placeholders = ','.join('?' for _ in columns)
    sql = f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders}) ON CONFLICT(animal_id) DO UPDATE SET "
    sql += ','.join(f'{name}=excluded.{name}' for name in [*names, 'version'])
    db.execute(sql, values)
    db.execute('INSERT INTO decision_history(subsystem,animal_id,version,state,reason,evidence_id) VALUES(?,?,?,?,?,?)',
               (table, animal_id, version, fields['state'], fields['reason'], evidence_id))
    return dict(db.execute(f'SELECT * FROM {table} WHERE animal_id=?', (animal_id,)).fetchone())


def update_claim_decision(db, claim_id, animal_id, fields, evidence_id=None):
    old = db.execute('SELECT * FROM medical_claim_decision WHERE claim_id=?', (claim_id,)).fetchone()
    names = list(fields)
    if old and all(old[name] == fields[name] for name in names):
        return dict(old)
    version = old['version'] + 1 if old else 1
    columns = ['claim_id', *names, 'version']
    values = [claim_id, *(fields[name] for name in names), version]
    placeholders = ','.join('?' for _ in columns)
    sql = f"INSERT INTO medical_claim_decision({','.join(columns)}) VALUES({placeholders}) ON CONFLICT(claim_id) DO UPDATE SET "
    sql += ','.join(f'{name}=excluded.{name}' for name in [*names, 'version'])
    db.execute(sql, values)
    db.execute('INSERT INTO claim_decision_history(claim_id,animal_id,version,state,reason,evidence_id) VALUES(?,?,?,?,?,?)',
               (claim_id, animal_id, version, fields['state'], fields['reason'], evidence_id))
    return dict(db.execute('SELECT * FROM medical_claim_decision WHERE claim_id=?', (claim_id,)).fetchone())
