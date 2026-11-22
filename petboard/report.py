import json


def report(db):
    animals = []
    for animal in db.execute('SELECT * FROM animal ORDER BY id'):
        item = dict(animal)
        for table, key in [('intake_decision', 'intake'), ('care_decision', 'care'), ('handoff_decision', 'handoff'), ('kennel_decision', 'kennel')]:
            row = db.execute(f'SELECT * FROM {table} WHERE animal_id=?', (animal['id'],)).fetchone()
            item[key] = dict(row) if row else None
        item['history'] = [dict(row) for row in db.execute('SELECT subsystem,version,state,reason,evidence_id FROM decision_history WHERE animal_id=? ORDER BY id', (animal['id'],))]
        item['fee_lines'] = [dict(row) for row in db.execute('SELECT kind,amount,basis,frozen FROM fee_line WHERE animal_id=? ORDER BY kind', (animal['id'],))]
        injury = db.execute('SELECT * FROM injury_case WHERE animal_id=?', (animal['id'],)).fetchone()
        item['injury_case'] = dict(injury) if injury else None
        item['injury_resolutions'] = [dict(row) for row in db.execute('SELECT * FROM injury_resolution WHERE animal_id=? ORDER BY event_id', (animal['id'],))]
        animals.append(item)
    return {'animals': animals, 'booking': booking(db),
            'evidence_count': db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
            'source_files': [dict(row) for row in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')]}


def booking(db):
    plans = []
    for plan in db.execute('SELECT * FROM booking_plan ORDER BY id'):
        plans.append({
            'id': plan['id'], 'on_time': plan['on_time'], 'intact_groups': plan['intact_groups'],
            'groups': [dict(row) for row in db.execute('SELECT group_id,status,detail FROM booking_plan_group WHERE plan_id=? ORDER BY group_id', (plan['id'],))],
            'items': [dict(row) for row in db.execute('SELECT group_id,animal_id,status,unit_id,starts,ends,detail FROM booking_plan_item WHERE plan_id=? ORDER BY group_id,animal_id,starts', (plan['id'],))]})
    return {
        'groups': [dict(row) for row in db.execute('SELECT * FROM booking_group ORDER BY id')],
        'members': [dict(row) for row in db.execute('SELECT * FROM booking_member ORDER BY event_id')],
        'constraints': [dict(row) for row in db.execute('SELECT * FROM booking_constraint ORDER BY event_id')],
        'handovers': [dict(row) for row in db.execute('SELECT * FROM booking_handover ORDER BY event_id')],
        'plans': plans}


def dumps(db):
    return json.dumps(report(db), ensure_ascii=False, indent=2)
