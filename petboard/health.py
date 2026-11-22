import json
from .source import record
from .store import update_decision


def decide(db, animal_id):
    animal = db.execute('SELECT * FROM animal WHERE id=?', (animal_id,)).fetchone()
    if not animal:
        return None
    rows = db.execute('SELECT * FROM health_certificate WHERE animal_id=? ORDER BY version DESC,event_id', (animal_id,)).fetchall()
    if not rows:
        result = ('pending', 'health certificate missing', None)
    else:
        top = rows[0]
        parallels = [r for r in rows if r['version'] == top['version']]
        content = {(r['chip'], r['vaccine_until'], r['contraindications'], r['condition']) for r in parallels}
        if len(content) > 1:
            result = ('frozen', 'parallel health version conflict', None)
        elif top['chip'] != animal_id or top['signed_by'] != 'veterinarian':
            result = ('rejected', 'animal identity or veterinarian signature invalid', None)
        elif top['vaccine_until'] < animal['departure']:
            result = ('rejected', 'vaccine expires during boarding', None)
        elif db.execute("SELECT 1 FROM certificate_revocation WHERE certificate_id=? AND signed_by='veterinarian'", (top['event_id'],)).fetchone():
            result = ('revoked', 'veterinarian revoked certificate', None)
        elif animal['requested_medicine'] in json.loads(top['contraindications']):
            result = ('rejected', 'requested medicine conflicts with veterinarian contraindication', None)
        else:
            result = ('accepted', 'identity, veterinarian and vaccine verified', top['event_id'])
    state, reason, certificate_id = result
    answer = update_decision(db, 'intake_decision', animal_id,
                             dict(state=state, reason=reason, certificate_id=certificate_id), certificate_id)
    from .care import decide as care_decide
    care_decide(db, animal_id)
    from .handoff import decide as handoff_decide
    handoff_decide(db, animal_id)
    from .kennel import decide as kennel_decide
    kennel_decide(db, animal_id)
    from .booking import replan
    replan(db)
    return answer


def import_data(db, data):
    touched = set()
    with db:
        for value in data.get('animals', []):
            if record(db, value['event_id'], 'animal', value):
                db.execute('INSERT INTO animal(id,owner,arrival,departure,daily_fee,service_fee,requested_medicine) VALUES(?,?,?,?,?,?,?)',
                           (value['id'], value['owner'], value['arrival'], value['departure'], value['daily_fee'], value['service_fee'], value.get('requested_medicine', '')))
            touched.add(value['id'])
        for value in data.get('certificates', []):
            if record(db, value['event_id'], 'certificate', value):
                db.execute('INSERT INTO health_certificate(event_id,animal_id,version,chip,signed_by,vaccine_until,contraindications,condition) VALUES(?,?,?,?,?,?,?,?)',
                           (value['event_id'], value['animal_id'], value['version'], value['chip'], value['signed_by'], value['vaccine_until'],
                            json.dumps(value.get('contraindications', []), ensure_ascii=False), value.get('condition', 'healthy')))
            touched.add(value['animal_id'])
        for value in data.get('revocations', []):
            certificate = db.execute('SELECT animal_id FROM health_certificate WHERE event_id=?', (value['certificate_id'],)).fetchone()
            if not certificate or certificate['animal_id'] != value['animal_id']:
                raise ValueError('revocation must identify existing certificate and animal')
            if record(db, value['event_id'], 'revocation', value):
                db.execute('INSERT INTO certificate_revocation(event_id,certificate_id,animal_id,at,signed_by,reason) VALUES(?,?,?,?,?,?)',
                           (value['event_id'], value['certificate_id'], value['animal_id'], value['at'], value['signed_by'], value['reason']))
            touched.add(value['animal_id'])
        return [decide(db, animal_id) for animal_id in sorted(touched)]
