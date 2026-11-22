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
    demand_studies = []
    for row in db.execute('SELECT day,experiment,metrics FROM demand_study_decision ORDER BY day,experiment'):
        item = dict(row)
        item['metrics'] = json.loads(row['metrics'])
        demand_studies.append(item)
    return {'animals': animals,
            'booking_days': [dict(row) for row in db.execute('SELECT * FROM booking_decision ORDER BY day')],
            'demand_days': [dict(row) for row in db.execute('SELECT * FROM demand_decision ORDER BY day')],
            'demand_studies': demand_studies,
            'price_snapshots': [dict(row) for row in db.execute('SELECT * FROM price_snapshot ORDER BY at,event_id')],
            'page_exposures': [dict(row) for row in db.execute('SELECT * FROM page_exposure ORDER BY at,event_id')],
            'evidence_count': db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
            'source_files': [dict(row) for row in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')]}


def dumps(db):
    return json.dumps(report(db), ensure_ascii=False, indent=2)
