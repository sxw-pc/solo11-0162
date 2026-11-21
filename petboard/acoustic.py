from .source import record


def decide(db, period):
    samples = db.execute('SELECT * FROM noise_sample WHERE period=? ORDER BY at,event_id', (period,)).fetchall()
    if not samples:
        return None
    peak = max(row['db'] for row in samples)
    state = 'exceeded' if peak >= 65 else 'clear'
    reason = 'single peak over boundary limit' if state == 'exceeded' else 'single peak within boundary limit'
    old = db.execute('SELECT * FROM noise_decision WHERE period=?', (period,)).fetchone()
    if not old or old['state'] != state or old['peak_db'] != peak:
        version = old['version'] + 1 if old else 1
        db.execute('INSERT INTO noise_decision(period,state,reason,peak_db,version) VALUES(?,?,?,?,?) ON CONFLICT(period) DO UPDATE SET state=excluded.state,reason=excluded.reason,peak_db=excluded.peak_db,version=excluded.version',
                   (period, state, reason, peak, version))
    from .complaint import decide as complaint_decide
    complaint_decide(db, period)
    return dict(db.execute('SELECT * FROM noise_decision WHERE period=?', (period,)).fetchone())


def import_data(db, data):
    touched = set()
    with db:
        for value in data.get('samples', []):
            if record(db, value['event_id'], 'noise_sample', value):
                db.execute('INSERT INTO noise_sample(event_id,period,sensor,at,db) VALUES(?,?,?,?,?)',
                           (value['event_id'], value['period'], value['sensor'], value['at'], value['db']))
            touched.add(value['period'])
        return [decide(db, period) for period in sorted(touched)]
