import json


def report(db):
    animals = []
    for animal in db.execute('SELECT * FROM animal ORDER BY id'):
        item = dict(animal)
        for table, key in [('intake_decision', 'intake'), ('care_decision', 'care'), ('handoff_decision', 'handoff')]:
            row = db.execute(f'SELECT * FROM {table} WHERE animal_id=?', (animal['id'],)).fetchone()
            item[key] = dict(row) if row else None
        item['history'] = [dict(row) for row in db.execute('SELECT subsystem,version,state,reason,evidence_id FROM decision_history WHERE animal_id=? ORDER BY id', (animal['id'],))]
        item['fee_lines'] = [dict(row) for row in db.execute('SELECT kind,amount,basis,frozen FROM fee_line WHERE animal_id=? ORDER BY kind', (animal['id'],))]
        injury = db.execute('SELECT * FROM injury_case WHERE animal_id=?', (animal['id'],)).fetchone()
        item['injury_case'] = dict(injury) if injury else None
        item['injury_resolutions'] = [dict(row) for row in db.execute('SELECT * FROM injury_resolution WHERE animal_id=? ORDER BY event_id', (animal['id'],))]
        item['arbitrations'] = [dict(row) for row in db.execute(
            'SELECT * FROM arbitration_evidence WHERE animal_id=? ORDER BY event_id', (animal['id'],))]
        item['arbitration_revocations'] = [dict(row) for row in db.execute(
            'SELECT * FROM arbitration_revocation WHERE animal_id=? ORDER BY event_id', (animal['id'],))]
        item['correction_revocations'] = [dict(row) for row in db.execute(
            'SELECT * FROM correction_revocation WHERE animal_id=? ORDER BY event_id', (animal['id'],))]
        item['care_chains'] = [dict(row, nodes=json.loads(row['nodes'])) for row in db.execute(
            """SELECT l.root_id,l.plan_id,l.kind,l.decision_version,l.status,l.adopted_tip_id,l.arbitration_id,l.nodes,l.reason
               FROM care_chain_ledger l
               JOIN (SELECT root_id, MAX(id) AS max_id FROM care_chain_ledger
                     WHERE animal_id=? AND decision_version=(SELECT MAX(decision_version) FROM care_chain_ledger WHERE animal_id=?)
                     GROUP BY root_id) t ON l.id=t.max_id ORDER BY l.root_id""",
            (animal['id'], animal['id']))]
        item['care_chain_history'] = [dict(row, nodes=json.loads(row['nodes'])) for row in db.execute(
            'SELECT root_id,plan_id,kind,decision_version,status,adopted_tip_id,arbitration_id,nodes,reason FROM care_chain_ledger WHERE animal_id=? ORDER BY id',
            (animal['id'],))]
        item['fee_line_history'] = [dict(row) for row in db.execute(
            'SELECT handoff_version,kind,amount,basis,frozen FROM fee_line_history WHERE animal_id=? ORDER BY id', (animal['id'],))]
        animals.append(item)
    return {'animals': animals, 'evidence_count': db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
            'source_files': [dict(row) for row in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')]}


def dumps(db):
    return json.dumps(report(db), ensure_ascii=False, indent=2)
