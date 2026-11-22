import json
from .source import record
from .store import update_decision
from . import vaccine_coldchain as coldchain


def import_data(db, data):
    touched_animals = set()
    with db:
        touched_vials = coldchain.record_events(db, data)
        for value in data.get('shots', []):
            if record(db, value['event_id'], 'vaccination_shot', value):
                db.execute('INSERT INTO vaccination_shot(event_id,animal_id,vial_id,dose_seq,at,signed_by,witness) VALUES(?,?,?,?,?,?,?)',
                           (value['event_id'], value['animal_id'], value['vial_id'], value['dose_seq'],
                            value['at'], value['signed_by'], value.get('witness')))
            touched_vials.add(value['vial_id'])
            touched_animals.add(value['animal_id'])
        for vial_id in sorted(touched_vials):
            decide_vial(db, vial_id)
        touched_animals.update(certificate_animals(db, touched_vials))
        from .health import decide as health_decide
        return [health_decide(db, animal_id) for animal_id in sorted(touched_animals)]


def certificate_animals(db, vial_ids):
    """Animals whose health certificates cite shots drawn from these vials."""
    if not vial_ids:
        return set()
    marks = ','.join('?' for _ in vial_ids)
    rows = db.execute(f'SELECT DISTINCT animal_id FROM health_certificate WHERE shot_id IN '
                      f'(SELECT event_id FROM vaccination_shot WHERE vial_id IN ({marks}))',
                      tuple(vial_ids)).fetchall()
    return {row['animal_id'] for row in rows}


def disposal_map(db, vial_id):
    """dose_seq -> disposal row, plus doses claimed by more than one disposal."""
    disposed, conflicts = {}, set()
    rows = db.execute('SELECT * FROM dose_disposal WHERE vial_id=? ORDER BY at,event_id', (vial_id,)).fetchall()
    for row in rows:
        for seq in json.loads(row['dose_seqs']):
            if seq in disposed:
                conflicts.add(seq)
            disposed.setdefault(seq, row)
    return disposed, conflicts


def shot_status(db, shot, vial, batch, context):
    """Qualify one recorded shot. The injection fact is never erased; only its
    qualification moves between valid, review and invalid."""
    if not vial:
        return 'review', 'vaccine vial not registered'
    if shot['dose_seq'] < 1 or shot['dose_seq'] > vial['nominal_doses']:
        return 'invalid', 'dose exceeds vial nominal doses'
    disposed, disposal_conflicts, shots_by_dose, opening = context
    if shot['dose_seq'] in disposal_conflicts:
        return 'review', 'dose claimed by conflicting disposal records'
    if shot['dose_seq'] in disposed:
        disposal = disposed[shot['dose_seq']]
        if disposal['at'] <= shot['at']:
            return 'invalid', 'dose discarded before administration'
        return 'review', 'disposal conflicts with recorded administration'
    if len(shots_by_dose.get(shot['dose_seq'], [])) > 1:
        return 'review', 'dose double-used by multiple certificates'
    if shot['signed_by'] != 'veterinarian' or not shot['witness'] or shot['witness'] == shot['signed_by']:
        return 'review', 'veterinarian sign-off or independent witness missing'
    if not opening:
        return 'review', 'vial opening not recorded'
    if shot['at'] < opening['opened_at']:
        return 'invalid', 'dose administered before vial opened'
    problems = coldchain.chain_problems(db, vial, batch, shot['at'])
    if problems:
        return 'review', '; '.join(problems)
    return 'valid', 'dose, cold chain and sign-off verified'


def decide_vial(db, vial_id):
    vial = db.execute('SELECT * FROM vaccine_vial WHERE vial_id=?', (vial_id,)).fetchone()
    shots = db.execute('SELECT * FROM vaccination_shot WHERE vial_id=? ORDER BY dose_seq,event_id', (vial_id,)).fetchall()
    if not vial:
        for shot in shots:
            upsert_shot(db, shot, 'review', 'vaccine vial not registered')
        return None
    batch = db.execute('SELECT * FROM vaccine_batch WHERE batch_id=?', (vial['batch_id'],)).fetchone()
    disposed, disposal_conflicts = disposal_map(db, vial_id)
    shots_by_dose = {}
    for shot in shots:
        shots_by_dose.setdefault(shot['dose_seq'], []).append(shot)
    opening = db.execute('SELECT * FROM vial_opening WHERE vial_id=? ORDER BY opened_at LIMIT 1', (vial_id,)).fetchone()
    context = (disposed, disposal_conflicts, shots_by_dose, opening)
    as_of = coldchain.vial_as_of(db, vial_id, vial['stored_from'])
    remaining_problems = coldchain.chain_problems(db, vial, batch, as_of)
    usable = administered = pending = discarded = 0
    disposition = []
    shot_verdicts = {}
    for seq in range(1, vial['nominal_doses'] + 1):
        entry = {'dose_seq': seq}
        dose_shots = shots_by_dose.get(seq, [])
        if seq in disposal_conflicts:
            pending += 1
            entry.update(state='pending', detail='conflicting disposal records')
        elif seq in disposed:
            disposal = disposed[seq]
            if dose_shots and min(shot['at'] for shot in dose_shots) < disposal['at']:
                pending += 1
                entry.update(state='pending', detail=f"disposal {disposal['event_id']} conflicts with recorded administration")
            else:
                discarded += 1
                entry.update(state='discarded',
                             detail=f"weighed {disposal['weight_grams']}g, handed to {disposal['handed_to']} at {disposal['at']}")
        elif dose_shots:
            if len(dose_shots) > 1:
                pending += 1
                entry.update(state='pending', detail='dose claimed by multiple shots: '
                             + ','.join(shot['event_id'] for shot in dose_shots))
            else:
                shot = dose_shots[0]
                state, reason = shot_status(db, shot, vial, batch, context)
                shot_verdicts[shot['event_id']] = (state, reason)
                administered += 1
                entry.update(state='administered',
                             detail=f"shot {shot['event_id']} to {shot['animal_id']} at {shot['at']} ({state})")
        elif remaining_problems:
            pending += 1
            entry.update(state='pending', detail='; '.join(remaining_problems))
        else:
            usable += 1
            entry.update(state='usable', detail='held in cold storage')
        disposition.append(entry)
    non_valid = 0
    for shot in shots:
        if shot['event_id'] in shot_verdicts:
            state, reason = shot_verdicts[shot['event_id']]
        elif shot['dose_seq'] < 1 or shot['dose_seq'] > vial['nominal_doses']:
            state, reason = 'invalid', 'dose exceeds vial nominal doses'
        else:
            state, reason = shot_status(db, shot, vial, batch, context)
        if state != 'valid':
            non_valid += 1
        upsert_shot(db, shot, state, reason)
    state = 'review' if pending or non_valid else 'ok'
    reason = (f'{pending} dose(s) under review' if pending else
              ('shot qualification under review' if non_valid else 'all doses accounted for'))
    return update_decision(db, 'vial_decision', vial_id,
                           dict(state=state, reason=reason, usable=usable, administered=administered,
                                pending=pending, discarded=discarded,
                                disposition=json.dumps(disposition, ensure_ascii=False)),
                           key='vial_id')


def upsert_shot(db, shot, state, reason):
    db.execute('INSERT INTO shot_decision(shot_id,animal_id,vial_id,dose_seq,state,reason) VALUES(?,?,?,?,?,?) '
               'ON CONFLICT(shot_id) DO UPDATE SET state=excluded.state,reason=excluded.reason',
               (shot['event_id'], shot['animal_id'], shot['vial_id'], shot['dose_seq'], state, reason))
