from .source import record
from .store import update_decision


def decide(db, animal_id):
    intake = db.execute('SELECT state FROM intake_decision WHERE animal_id=?', (animal_id,)).fetchone()
    rows = db.execute('SELECT * FROM kennel_placement WHERE animal_id=? ORDER BY starts DESC,event_id DESC', (animal_id,)).fetchall()
    if not rows:
        state, reason, unit_id = 'pending', 'placement evidence missing', None
    else:
        place = rows[0]
        unit_id = place['unit_id']
        unit = db.execute('SELECT * FROM kennel_unit WHERE id=?', (unit_id,)).fetchone()
        overlap = db.execute('SELECT * FROM kennel_placement WHERE unit_id=? AND starts<? AND ends>? ORDER BY starts,event_id',
                             (unit_id, place['ends'], place['starts'])).fetchall()
        double = db.execute('SELECT 1 FROM kennel_placement WHERE animal_id=? AND event_id<>? AND starts<? AND ends>?',
                            (animal_id, place['event_id'], place['ends'], place['starts'])).fetchone()
        points = sorted({p['starts'] for p in overlap} | {p['ends'] for p in overlap})
        peak = 0
        for s, e in zip(points, points[1:]):
            peak = max(peak, len({p['animal_id'] for p in overlap if p['starts'] < e and s < p['ends']}))
        if not unit:
            state, reason = 'blocked', 'kennel unit missing'
        elif not intake or intake['state'] != 'accepted':
            state, reason = 'blocked', 'intake qualification not accepted'
        elif double:
            state, reason = 'conflict', 'animal placed in two kennels at the same time'
        elif peak > unit['capacity']:
            state, reason = 'conflict', 'kennel capacity exceeded'
        else:
            state, reason = 'placed', 'accepted animal within kennel capacity'
    return update_decision(db, 'kennel_decision', animal_id,
                           dict(state=state, reason=reason, unit_id=unit_id))


def import_data(db, data):
    touched = set()
    with db:
        for value in data.get('units', []):
            if record(db, value['event_id'], 'kennel_unit', value):
                db.execute('INSERT INTO kennel_unit(id,zone,capacity) VALUES(?,?,?)',
                           (value['id'], value['zone'], value['capacity']))
        for value in data.get('placements', []):
            if value['starts'] >= value['ends']:
                raise ValueError('placement must have positive interval')
            if record(db, value['event_id'], 'kennel_placement', value):
                db.execute('INSERT INTO kennel_placement(event_id,animal_id,unit_id,starts,ends,actor) VALUES(?,?,?,?,?,?)',
                           (value['event_id'], value['animal_id'], value['unit_id'], value['starts'], value['ends'], value['actor']))
            touched.add(value['animal_id'])
        results = [decide(db, animal_id) for animal_id in sorted(touched)]
        from .booking import replan
        replan(db)
        return results
