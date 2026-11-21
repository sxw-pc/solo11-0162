from .source import record


def decide(db, period):
    count = db.execute('SELECT COUNT(*) FROM neighbor_complaint WHERE period=?', (period,)).fetchone()[0]
    if not count:
        return None
    noise = db.execute('SELECT * FROM noise_decision WHERE period=?', (period,)).fetchone()
    state = 'upheld' if noise and noise['state'] == 'exceeded' else 'pending' if not noise else 'dismissed'
    reason = 'boundary reading exceeds threshold' if state == 'upheld' else 'boundary reading unavailable' if state == 'pending' else 'boundary reading below threshold'
    old = db.execute('SELECT * FROM complaint_decision WHERE period=?', (period,)).fetchone()
    if not old or old['state'] != state or old['complaint_count'] != count:
        version = old['version'] + 1 if old else 1
        db.execute('INSERT INTO complaint_decision(period,state,reason,complaint_count,version) VALUES(?,?,?,?,?) ON CONFLICT(period) DO UPDATE SET state=excluded.state,reason=excluded.reason,complaint_count=excluded.complaint_count,version=excluded.version',
                   (period, state, reason, count, version))
    return dict(db.execute('SELECT * FROM complaint_decision WHERE period=?', (period,)).fetchone())


def import_data(db, data):
    touched = set()
    with db:
        for value in data.get('complaints', []):
            if record(db, value['event_id'], 'neighbor_complaint', value):
                db.execute('INSERT INTO neighbor_complaint(event_id,period,claimant,address,claim) VALUES(?,?,?,?,?)',
                           (value['event_id'], value['period'], value['claimant'], value['address'], value['claim']))
            touched.add(value['period'])
        return [decide(db, period) for period in sorted(touched)]
