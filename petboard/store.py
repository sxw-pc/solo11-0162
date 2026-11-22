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
CREATE TABLE IF NOT EXISTS vaccine_batch(event_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, manufacturer TEXT NOT NULL, vaccine TEXT NOT NULL, min_temp REAL NOT NULL, max_temp REAL NOT NULL, open_expiry_hours INTEGER NOT NULL, received_at TEXT NOT NULL, signed_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vaccine_vial(event_id TEXT PRIMARY KEY, vial_id TEXT NOT NULL UNIQUE, batch_id TEXT NOT NULL, nominal_doses INTEGER NOT NULL, stored_from TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS temperature_packet(event_id TEXT PRIMARY KEY, vial_id TEXT NOT NULL, logger_id TEXT NOT NULL, from_ts TEXT NOT NULL, to_ts TEXT NOT NULL, readings TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS logger_calibration(event_id TEXT PRIMARY KEY, logger_id TEXT NOT NULL, offset REAL NOT NULL, effective_from TEXT NOT NULL, effective_to TEXT NOT NULL, signed_by TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vial_opening(event_id TEXT PRIMARY KEY, vial_id TEXT NOT NULL, opened_at TEXT NOT NULL, opened_by TEXT NOT NULL, witness TEXT, retention_sample TEXT);
CREATE TABLE IF NOT EXISTS stability_review(event_id TEXT PRIMARY KEY, vial_id TEXT NOT NULL, anomaly_from TEXT NOT NULL, anomaly_to TEXT NOT NULL, covers_max_temp REAL, covers_min_temp REAL, covers_max_hours REAL NOT NULL, retest TEXT NOT NULL, retention_sample TEXT, signed_by TEXT NOT NULL, manufacturer_signed_by TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dose_disposal(event_id TEXT PRIMARY KEY, vial_id TEXT NOT NULL, dose_seqs TEXT NOT NULL, weight_grams REAL NOT NULL, handed_to TEXT NOT NULL, witness TEXT, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vaccination_shot(event_id TEXT PRIMARY KEY, animal_id TEXT NOT NULL, vial_id TEXT NOT NULL, dose_seq INTEGER NOT NULL, at TEXT NOT NULL, signed_by TEXT NOT NULL, witness TEXT);
CREATE TABLE IF NOT EXISTS shot_decision(shot_id TEXT PRIMARY KEY, animal_id TEXT NOT NULL, vial_id TEXT NOT NULL, dose_seq INTEGER NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vial_decision(vial_id TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT NOT NULL, usable INTEGER NOT NULL, administered INTEGER NOT NULL, pending INTEGER NOT NULL, discarded INTEGER NOT NULL, disposition TEXT NOT NULL, version INTEGER NOT NULL);
"""


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    ensure_column(db, 'health_certificate', 'shot_id', "TEXT NOT NULL DEFAULT ''")
    return db


def ensure_column(db, table, column, ddl):
    if column not in {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}:
        db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}')


def update_decision(db, table, animal_id, fields, evidence_id=None, key='animal_id'):
    old = db.execute(f"SELECT * FROM {table} WHERE {key}=?", (animal_id,)).fetchone()
    names = list(fields)
    if old and all(old[name] == fields[name] for name in names):
        return dict(old)
    version = old['version'] + 1 if old else 1
    columns = [key, *names, 'version']
    values = [animal_id, *(fields[name] for name in names), version]
    placeholders = ','.join('?' for _ in columns)
    sql = f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders}) ON CONFLICT({key}) DO UPDATE SET "
    sql += ','.join(f'{name}=excluded.{name}' for name in [*names, 'version'])
    db.execute(sql, values)
    db.execute('INSERT INTO decision_history(subsystem,animal_id,version,state,reason,evidence_id) VALUES(?,?,?,?,?,?)',
               (table, animal_id, version, fields['state'], fields['reason'], evidence_id))
    return dict(db.execute(f'SELECT * FROM {table} WHERE {key}=?', (animal_id,)).fetchone())
