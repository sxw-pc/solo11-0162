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
        for table, key in [('skill_decision', 'training_skill'), ('graduation_decision', 'training_graduation')]:
            row = db.execute(f'SELECT * FROM {table} WHERE animal_id=?', (animal['id'],)).fetchone()
            item[key] = dict(row) if row else None
        item['training_trials'] = [dict(row) for row in db.execute('SELECT * FROM training_trial WHERE animal_id=? ORDER BY at,event_id', (animal['id'],))]
        item['video_clips'] = [dict(row) for row in db.execute(
            'SELECT clip_id,path,sha256,recorded_at,duration_ms,frame_count FROM video_clip ORDER BY clip_id')]
        item['recall_trials'] = [dict(row) for row in db.execute(
            'SELECT * FROM recall_trial WHERE animal_id=? ORDER BY started_at,event_id', (animal['id'],))]
        item['blind_protocols'] = [dict(row) for row in db.execute(
            'SELECT * FROM blind_protocol WHERE animal_id=? ORDER BY registered_at,protocol_id', (animal['id'],))]
        item['blind_reviews'] = [dict(row) for row in db.execute(
            'SELECT * FROM blind_review WHERE animal_id=? ORDER BY reviewed_at,event_id', (animal['id'],))]
        item['welfare_events'] = [dict(row) for row in db.execute(
            'SELECT * FROM welfare_event WHERE animal_id=? ORDER BY at,event_id', (animal['id'],))]
        item['scope_credentials'] = [dict(row) for row in db.execute(
            'SELECT * FROM scope_credential WHERE animal_id=? ORDER BY scope', (animal['id'],))]
        animals.append(item)
    return {'animals': animals, 'evidence_count': db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
            'source_files': [dict(row) for row in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')]}


def dumps(db):
    return json.dumps(report(db), ensure_ascii=False, indent=2)
