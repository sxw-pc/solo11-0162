import json
from .source import record
from .store import update_decision


def decide(db, animal_id):
    intake = db.execute('SELECT * FROM intake_decision WHERE animal_id=?', (animal_id,)).fetchone()
    plans = db.execute('SELECT * FROM care_plan WHERE animal_id=? ORDER BY version DESC,event_id', (animal_id,)).fetchall()
    if not plans:
        state, reason, missed, compensation = 'pending', 'care plan missing', 0, 0
    else:
        plan = plans[0]
        parallels = [r for r in plans if r['version'] == plan['version']]
        certificate = db.execute('SELECT * FROM health_certificate WHERE event_id=?', (intake['certificate_id'],)).fetchone() if intake and intake['certificate_id'] else None
        forbidden = set(json.loads(certificate['contraindications'])) if certificate else set()
        actions = db.execute('SELECT * FROM care_action WHERE animal_id=? AND plan_id=? ORDER BY day,event_id', (animal_id, plan['event_id'])).fetchall()
        replaced = {r['supersedes'] for r in actions if r['supersedes']}
        actions = [r for r in actions if r['event_id'] not in replaced]
        meals = [r for r in actions if r['kind'] == 'meal']
        medicines = [r for r in actions if r['kind'] == 'medicine']
        missed = max(0, plan['meals'] - len({r['event_id'] for r in meals if r['witness'] and r['witness'] != r['actor']}))
        animal = db.execute('SELECT * FROM animal WHERE id=?', (animal_id,)).fetchone()
        compensation = missed * animal['service_fee'] if animal else 0
        if not intake or intake['state'] != 'accepted':
            state, reason = 'blocked', 'animal health intake not accepted'
        elif len({(r['food'], r['medicine'], r['meals']) for r in parallels}) > 1:
            state, reason = 'frozen', 'parallel plan version conflict'
        elif plan['approved_by'] != 'owner':
            state, reason = 'blocked', 'owner has not approved plan'
        elif plan['food'] in forbidden or (plan['medicine'] and plan['medicine'] in forbidden):
            state, reason = 'blocked', 'plan contains veterinarian contraindication'
        elif any(r['medicine'] != plan['medicine'] or not r['witness'] or r['witness'] == r['actor'] for r in medicines):
            state, reason = 'disputed', 'medicine identity or independent witness missing'
        elif plan['medicine'] and not medicines:
            state, reason = 'partial', 'prescribed medicine has no witnessed action'
        elif any(r['food'] != plan['food'] for r in meals):
            state, reason = 'disputed', 'meal differs from approved plan'
        elif missed:
            state, reason = 'partial', 'missed meal compensation due'
        else:
            state, reason = 'fulfilled', 'approved plan and witnessed care fulfilled'
    result = update_decision(db, 'care_decision', animal_id,
                             dict(state=state, reason=reason, missed_meals=missed, compensation=compensation))
    from .handoff import decide as handoff_decide
    handoff_decide(db, animal_id)
    return result


def import_data(db, data):
    touched = set()
    with db:
        for value in data.get('plans', []):
            if record(db, value['event_id'], 'plan', value):
                db.execute('INSERT INTO care_plan(event_id,animal_id,version,food,medicine,meals,approved_by) VALUES(?,?,?,?,?,?,?)',
                           (value['event_id'], value['animal_id'], value['version'], value['food'], value.get('medicine', ''), value['meals'], value['approved_by']))
            touched.add(value['animal_id'])
        for value in data.get('actions', []):
            if value.get('supersedes'):
                old = db.execute('SELECT animal_id,plan_id,kind FROM care_action WHERE event_id=?', (value['supersedes'],)).fetchone()
                if not old or (old['animal_id'],old['plan_id'],old['kind']) != (value['animal_id'],value['plan_id'],value['kind']):
                    raise ValueError('correction must supersede a matching action')
            if record(db, value['event_id'], 'action', value):
                db.execute('INSERT INTO care_action(event_id,animal_id,plan_id,kind,day,actor,witness,food,medicine,supersedes) VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (value['event_id'], value['animal_id'], value['plan_id'], value['kind'], value['day'], value['actor'], value.get('witness'), value.get('food'), value.get('medicine'), value.get('supersedes')))
            touched.add(value['animal_id'])
        return [decide(db, animal_id) for animal_id in sorted(touched)]
