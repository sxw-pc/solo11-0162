from .source import record
from .store import update_decision


def decide(db, animal_id):
    trial = db.execute('SELECT * FROM training_trial WHERE animal_id=? ORDER BY at DESC,event_id DESC LIMIT 1', (animal_id,)).fetchone()
    if not trial:
        return None
    score = trial['successes'] / trial['attempts'] if trial['attempts'] > 0 else 0.0
    fields = dict(state='passed' if score >= 0.8 else 'practice', reason='latest trial success ratio', skill=trial['skill'], score=score)
    result = update_decision(db, 'skill_decision', animal_id, fields, trial['event_id'])
    from .graduation import decide as graduation_decide
    graduation_decide(db, animal_id)
    return result


def import_data(db, data):
    touched = set()
    with db:
        for value in data.get('trials', []):
            if record(db, value['event_id'], 'training_trial', value):
                db.execute('INSERT INTO training_trial(event_id,animal_id,at,skill,setting,handler,successes,attempts,video) VALUES(?,?,?,?,?,?,?,?,?)',
                           (value['event_id'], value['animal_id'], value['at'], value['skill'], value['setting'], value['handler'],
                            value['successes'], value['attempts'], value['video']))
            touched.add(value['animal_id'])
        return [decide(db, animal_id) for animal_id in sorted(touched)]
