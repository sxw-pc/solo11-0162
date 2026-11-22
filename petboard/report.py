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
        item['vaccination'] = [dict(row) for row in db.execute('SELECT * FROM shot_decision WHERE animal_id=? ORDER BY shot_id', (animal['id'],))]
        animals.append(item)
    vials = []
    for row in db.execute('SELECT * FROM vial_decision ORDER BY vial_id'):
        item = dict(row)
        item['disposition'] = json.loads(item['disposition'])
        vials.append(item)
    batches = []
    for batch in db.execute('SELECT * FROM vaccine_batch ORDER BY batch_id'):
        item = dict(batch)
        for field in ('usable', 'administered', 'pending', 'discarded'):
            item[field] = db.execute(f'SELECT COALESCE(SUM({field}),0) FROM vial_decision '
                                     'WHERE vial_id IN (SELECT vial_id FROM vaccine_vial WHERE batch_id=?)',
                                     (batch['batch_id'],)).fetchone()[0]
        batches.append(item)
    return {'animals': animals, 'vials': vials, 'batches': batches,
            'evidence_count': db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
            'source_files': [dict(row) for row in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')]}


def dumps(db):
    return json.dumps(report(db), ensure_ascii=False, indent=2)
