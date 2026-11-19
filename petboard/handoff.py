from datetime import date
from .source import record
from .store import update_decision


def decide(db, animal_id):
    animal = db.execute('SELECT * FROM animal WHERE id=?', (animal_id,)).fetchone()
    if not animal:
        return None
    intake = db.execute('SELECT * FROM intake_decision WHERE animal_id=?', (animal_id,)).fetchone()
    care = db.execute('SELECT * FROM care_decision WHERE animal_id=?', (animal_id,)).fetchone()
    handoffs = db.execute('SELECT * FROM handoff WHERE animal_id=? ORDER BY at DESC,event_id', (animal_id,)).fetchall()
    if not handoffs:
        state, reason, overdue, frozen, payable = 'pending', 'handoff evidence missing', 0, 0, 0
    else:
        handoff = handoffs[0]
        authority = db.execute('SELECT * FROM pickup_authority WHERE animal_id=? AND person=? AND valid_from<=? AND valid_to>=? ORDER BY valid_from DESC',
                               (animal_id, handoff['person'], handoff['at'], handoff['at'])).fetchone()
        overdue_days = max(0, (date.fromisoformat(handoff['at'][:10]) - date.fromisoformat(animal['departure'][:10])).days)
        overdue = overdue_days * animal['daily_fee']
        compensation = care['compensation'] if care else 0
        base = animal['daily_fee'] * max(1, (date.fromisoformat(animal['departure'][:10]) - date.fromisoformat(animal['arrival'][:10])).days)
        frozen = 0
        payable = max(0, base + overdue - compensation)
        cert = db.execute('SELECT condition FROM health_certificate WHERE event_id=?', (intake['certificate_id'],)).fetchone() if intake and intake['certificate_id'] else None
        if not authority or authority['signed_by'] != animal['owner'] or not handoff['witness'] or handoff['witness'] == handoff['person']:
            state, reason = 'blocked', 'authorized pickup or independent witness missing'
            frozen, payable = payable, 0
        elif handoff['condition'] != (cert['condition'] if cert else 'healthy') and not handoff['veterinarian']:
            state, reason = 'disputed', 'new health condition lacks veterinarian evidence'
            frozen, payable = payable, 0
        elif handoff['dispute'] or (care and care['state'] in ('blocked', 'disputed', 'frozen')):
            state, reason = 'disputed', 'care or handoff dispute prevents final settlement'
            frozen, payable = payable, 0
        elif not intake or intake['state'] != 'accepted':
            state, reason = 'blocked', 'intake qualification invalid'
            frozen, payable = payable, 0
        else:
            state, reason = 'settled', 'authorized pickup and care terms reconciled'
    return update_decision(db, 'handoff_decision', animal_id,
                           dict(state=state, reason=reason, overdue_fee=overdue, frozen_fee=frozen, payable_fee=payable))


def import_data(db, data):
    touched = set()
    with db:
        for value in data.get('authorities', []):
            if record(db, value['event_id'], 'authority', value):
                db.execute('INSERT INTO pickup_authority(event_id,animal_id,person,valid_from,valid_to,signed_by) VALUES(?,?,?,?,?,?)',
                           (value['event_id'], value['animal_id'], value['person'], value['valid_from'], value['valid_to'], value['signed_by']))
            touched.add(value['animal_id'])
        for value in data.get('handoffs', []):
            if record(db, value['event_id'], 'handoff', value):
                db.execute('INSERT INTO handoff(event_id,animal_id,at,person,witness,condition,veterinarian,dispute) VALUES(?,?,?,?,?,?,?,?)',
                           (value['event_id'], value['animal_id'], value['at'], value['person'], value.get('witness'), value['condition'], value.get('veterinarian'), value.get('dispute')))
            touched.add(value['animal_id'])
        return [decide(db, animal_id) for animal_id in sorted(touched)]
