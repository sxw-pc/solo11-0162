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
        item['medical_claims'] = []
        for claim in db.execute('SELECT * FROM medical_claim WHERE animal_id=? ORDER BY claim_id', (animal['id'],)):
            entry = dict(claim)
            entry['decision'] = _row(db.execute('SELECT * FROM medical_claim_decision WHERE claim_id=?', (claim['claim_id'],)).fetchone())
            entry['policy'] = _row(db.execute('SELECT * FROM medical_policy WHERE claim_id=?', (claim['claim_id'],)).fetchone())
            entry['bill_lines'] = [dict(row) for row in db.execute(
                'SELECT event_id,line_no,chip,hospital,visit_at,description,amount FROM hospital_bill WHERE claim_id=? ORDER BY line_no,event_id',
                (claim['claim_id'],))]
            entry['approvals'] = [dict(row) for row in db.execute(
                'SELECT event_id,at,reviewer,decision,approved_amount,note FROM insurance_approval WHERE claim_id=? ORDER BY at,event_id',
                (claim['claim_id'],))]
            entry['transfers'] = [dict(row) for row in db.execute(
                'SELECT event_id,transaction_id,payer,payee,amount,at,status,reverses,memo FROM bank_transfer WHERE claim_id=? ORDER BY at,event_id',
                (claim['claim_id'],))]
            entry['recovery_items'] = [dict(row) for row in db.execute(
                'SELECT party,amount,reason,basis,status FROM recovery_item WHERE claim_id=? ORDER BY id',
                (claim['claim_id'],))]
            item['medical_claims'].append(entry)
        animals.append(item)
    return {'animals': animals, 'evidence_count': db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
            'source_files': [dict(row) for row in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')]}


def _row(row):
    return dict(row) if row else None


def dumps(db):
    return json.dumps(report(db), ensure_ascii=False, indent=2)
