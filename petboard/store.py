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
CREATE TABLE IF NOT EXISTS correction_revocation(event_id TEXT PRIMARY KEY, correction_id TEXT NOT NULL, animal_id TEXT NOT NULL, at TEXT NOT NULL, signed_by TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS arbitration_evidence(event_id TEXT PRIMARY KEY, animal_id TEXT NOT NULL, root_id TEXT NOT NULL, decision TEXT NOT NULL, tip_id TEXT, signed_by TEXT NOT NULL, witness TEXT, at TEXT NOT NULL, rationale TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS arbitration_revocation(event_id TEXT PRIMARY KEY, arbitration_id TEXT NOT NULL, animal_id TEXT NOT NULL, at TEXT NOT NULL, signed_by TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS care_chain_ledger(id INTEGER PRIMARY KEY, animal_id TEXT NOT NULL, root_id TEXT, plan_id TEXT NOT NULL, kind TEXT NOT NULL, decision_version INTEGER NOT NULL, status TEXT NOT NULL, adopted_tip_id TEXT, arbitration_id TEXT, nodes TEXT NOT NULL, reason TEXT NOT NULL, at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS care_decision(animal_id TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT NOT NULL, missed_meals INTEGER NOT NULL, compensation INTEGER NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS pickup_authority(event_id TEXT PRIMARY KEY, animal_id TEXT NOT NULL, person TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT NOT NULL, signed_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS handoff(event_id TEXT PRIMARY KEY, animal_id TEXT NOT NULL, at TEXT NOT NULL, person TEXT NOT NULL, witness TEXT, condition TEXT NOT NULL, veterinarian TEXT, dispute TEXT);
CREATE TABLE IF NOT EXISTS injury_case(animal_id TEXT PRIMARY KEY, intake_condition TEXT NOT NULL, return_condition TEXT NOT NULL, evidence_id TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS injury_resolution(event_id TEXT PRIMARY KEY, animal_id TEXT NOT NULL, handoff_id TEXT NOT NULL, veterinarian TEXT NOT NULL, owner TEXT NOT NULL, decision TEXT NOT NULL, rationale TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fee_line(animal_id TEXT NOT NULL, kind TEXT NOT NULL, amount INTEGER NOT NULL, basis TEXT NOT NULL, frozen INTEGER NOT NULL, PRIMARY KEY(animal_id,kind));
CREATE TABLE IF NOT EXISTS fee_line_history(id INTEGER PRIMARY KEY, animal_id TEXT NOT NULL, handoff_version INTEGER NOT NULL, kind TEXT NOT NULL, amount INTEGER NOT NULL, basis TEXT NOT NULL, frozen INTEGER NOT NULL, at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS handoff_decision(animal_id TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT NOT NULL, overdue_fee INTEGER NOT NULL, frozen_fee INTEGER NOT NULL, payable_fee INTEGER NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS decision_history(id INTEGER PRIMARY KEY, subsystem TEXT NOT NULL, animal_id TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL, evidence_id TEXT, at TEXT DEFAULT CURRENT_TIMESTAMP);
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


def archive_fee_lines(db, animal_id, handoff_version):
    """Append an immutable snapshot of the current fee lines before they are rewritten."""
    rows = db.execute('SELECT kind,amount,basis,frozen FROM fee_line WHERE animal_id=? ORDER BY kind', (animal_id,)).fetchall()
    for row in rows:
        db.execute('INSERT INTO fee_line_history(animal_id,handoff_version,kind,amount,basis,frozen) VALUES(?,?,?,?,?,?)',
                   (animal_id, handoff_version, row['kind'], row['amount'], row['basis'], row['frozen']))
