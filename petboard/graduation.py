from .store import update_decision


def decide(db, animal_id):
    skill = db.execute('SELECT * FROM skill_decision WHERE animal_id=?', (animal_id,)).fetchone()
    if not skill:
        return None
    eligible = skill['state'] == 'passed'
    fields = dict(state='graduated' if eligible else 'practice', reason='latest measured skill threshold',
                  credential=skill['skill'] if eligible else '')
    return update_decision(db, 'graduation_decision', animal_id, fields)
