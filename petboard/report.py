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
        animals.append(item)
    periods = sorted({row['period'] for row in db.execute('SELECT period FROM noise_sample')} | {row['period'] for row in db.execute('SELECT period FROM neighbor_complaint')})
    noise_cases = []
    for period in periods:
        noise = db.execute('SELECT * FROM noise_decision WHERE period=?', (period,)).fetchone()
        complaint = db.execute('SELECT * FROM complaint_decision WHERE period=?', (period,)).fetchone()
        noise_cases.append({'period': period, 'noise': dict(noise) if noise else None,
                            'complaint': dict(complaint) if complaint else None,
                            'samples': [dict(row) for row in db.execute('SELECT * FROM noise_sample WHERE period=? ORDER BY at,event_id', (period,))],
                            'complaints': [dict(row) for row in db.execute('SELECT * FROM neighbor_complaint WHERE period=? ORDER BY event_id', (period,))]})
    return {'animals': animals, 'noise_cases': noise_cases, 'evidence_count': db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
            'source_files': [dict(row) for row in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')]}


def dumps(db):
    return json.dumps(report(db), ensure_ascii=False, indent=2)
