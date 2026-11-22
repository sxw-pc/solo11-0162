from datetime import date
from .source import record
from .store import update_decision, archive_fee_lines


def decide(db, animal_id):
    animal = db.execute('SELECT * FROM animal WHERE id=?', (animal_id,)).fetchone()
    if not animal:
        return None
    intake = db.execute('SELECT * FROM intake_decision WHERE animal_id=?', (animal_id,)).fetchone()
    care = db.execute('SELECT * FROM care_decision WHERE animal_id=?', (animal_id,)).fetchone()
    handoffs = db.execute('SELECT * FROM handoff WHERE animal_id=? ORDER BY at DESC,event_id', (animal_id,)).fetchall()
    if not handoffs:
        return update_decision(db, 'handoff_decision', animal_id,
                               dict(state='pending', reason='handoff evidence missing',
                                    overdue_fee=0, frozen_fee=0, payable_fee=0))
    handoff = handoffs[0]
    handoff_day = handoff['at'][:10]
    authority = db.execute('SELECT * FROM pickup_authority WHERE animal_id=? AND person=? AND valid_from<=? AND valid_to>=? ORDER BY valid_from DESC',
                           (animal_id, handoff['person'], handoff_day, handoff_day)).fetchone()
    overdue_days = max(0, (date.fromisoformat(handoff['at'][:10]) - date.fromisoformat(animal['departure'][:10])).days)
    overdue = overdue_days * animal['daily_fee']
    compensation = care['compensation'] if care else 0
    base = animal['daily_fee'] * max(1, (date.fromisoformat(animal['departure'][:10]) - date.fromisoformat(animal['arrival'][:10])).days)
    frozen = 0
    payable = max(0, base + overdue - compensation)
    cert = db.execute('SELECT condition FROM health_certificate WHERE event_id=?', (intake['certificate_id'],)).fetchone() if intake and intake['certificate_id'] else None
    new_condition = bool(cert and handoff['condition'] != cert['condition'])
    resolution = db.execute('SELECT * FROM injury_resolution WHERE animal_id=? AND handoff_id=? ORDER BY event_id DESC LIMIT 1',
                            (animal_id, handoff['event_id'])).fetchone()
    cleared = bool(resolution and resolution['veterinarian'] != resolution['owner']
                   and resolution['owner'] == animal['owner'] and resolution['decision'] == 'preexisting')
    if new_condition:
        injury_state = 'cleared' if cleared else ('documented' if handoff['veterinarian'] else 'unverified')
        injury_reason = 'independently resolved as preexisting' if cleared else ('new symptom requires responsibility review' if handoff['veterinarian'] else 'new symptom lacks veterinarian evidence')
        db.execute('INSERT INTO injury_case(animal_id,intake_condition,return_condition,evidence_id,state,reason) VALUES(?,?,?,?,?,?) ON CONFLICT(animal_id) DO UPDATE SET return_condition=excluded.return_condition,evidence_id=excluded.evidence_id,state=excluded.state,reason=excluded.reason',
                   (animal_id, cert['condition'], handoff['condition'], handoff['event_id'], injury_state, injury_reason))
    else:
        db.execute('DELETE FROM injury_case WHERE animal_id=?', (animal_id,))
    if not authority or authority['signed_by'] != animal['owner'] or not handoff['witness'] or handoff['witness'] == handoff['person']:
        state, reason = 'blocked', 'authorized pickup or independent witness missing'
        frozen, payable = payable, 0
    elif new_condition and not cleared:
        state, reason = 'disputed', injury_reason
        frozen, payable = payable, 0
    elif handoff['dispute'] or (care and care['state'] in ('blocked', 'disputed', 'frozen')):
        state, reason = 'disputed', 'care or handoff dispute prevents final settlement'
        frozen, payable = payable, 0
    elif not intake or intake['state'] != 'accepted':
        state, reason = 'blocked', 'intake qualification invalid'
        frozen, payable = payable, 0
    else:
        state, reason = 'settled', 'authorized pickup and care terms reconciled'
    fields = dict(state=state, reason=reason, overdue_fee=overdue, frozen_fee=frozen, payable_fee=payable)
    old_handoff = db.execute('SELECT * FROM handoff_decision WHERE animal_id=?', (animal_id,)).fetchone()
    changed = not old_handoff or any(old_handoff[name] != fields[name] for name in fields)
    if changed:
        # Preserve the previous settlement lines before the fee rewrite so that
        # revoked arbitrations and reversed credits remain traceable.
        archive_fee_lines(db, animal_id, (old_handoff['version'] + 1 if old_handoff else 1))
        db.execute('DELETE FROM fee_line WHERE animal_id=?', (animal_id,))
        for kind, amount, basis in [('boarding', base, 'contracted arrival and departure dates'),
                                    ('overdue', overdue, 'confirmed pickup day minus contracted departure'),
                                    ('care_credit', -compensation, 'missed witnessed meals times service fee')]:
            db.execute('INSERT INTO fee_line(animal_id,kind,amount,basis,frozen) VALUES(?,?,?,?,?)',
                       (animal_id, kind, amount, basis, 1 if state != 'settled' else 0))
    return update_decision(db, 'handoff_decision', animal_id, fields)


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
        for value in data.get('resolutions', []):
            handoff = db.execute('SELECT animal_id FROM handoff WHERE event_id=?', (value['handoff_id'],)).fetchone()
            if not handoff or handoff['animal_id'] != value['animal_id']:
                raise ValueError('injury resolution must cite existing handoff for same animal')
            if value['decision'] not in ('preexisting', 'boarding_responsibility'):
                raise ValueError('unsupported injury responsibility decision')
            if record(db, value['event_id'], 'injury_resolution', value):
                db.execute('INSERT INTO injury_resolution(event_id,animal_id,handoff_id,veterinarian,owner,decision,rationale) VALUES(?,?,?,?,?,?,?)',
                           (value['event_id'], value['animal_id'], value['handoff_id'], value['veterinarian'], value['owner'], value['decision'], value['rationale']))
            touched.add(value['animal_id'])
        return [decide(db, animal_id) for animal_id in sorted(touched)]
