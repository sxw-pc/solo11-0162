import json
from .acoustic import LIMIT
from .source import record
from .store import update_period_decision


def decide(db, period):
    count = db.execute('SELECT COUNT(*) FROM neighbor_complaint WHERE period=?', (period,)).fetchone()[0]
    if not count:
        return None
    noise = db.execute('SELECT * FROM noise_decision WHERE period=?', (period,)).fetchone()
    if noise and noise['state'] == 'exceeded':
        state, reason = 'upheld', 'attributed store dose exceeds boundary limit'
    elif noise and noise['state'] == 'clear':
        state, reason = 'dismissed', 'attributed store dose within boundary limit'
    elif noise:
        state, reason = 'pending', 'source attribution unresolved; controlled isolation experiment required'
    else:
        state, reason = 'pending', 'boundary reading unavailable'
    # The decision must list the adopted windows, store dose, background
    # deduction basis and the uncertain parts; while uncertain it must also
    # ship an executable shutdown/barrier control plan instead of a verdict.
    basis = noise['basis'] if noise and state in ('upheld', 'dismissed') else None
    plan = _plan(db, period, noise) if state == 'pending' and noise else None
    answer = update_period_decision(db, 'complaint_decision', period,
                                    dict(state=state, reason=reason, complaint_count=count, basis=basis,
                                         required_plan=json.dumps(plan, ensure_ascii=False) if plan else None))
    decide_scope(db, period)
    return answer


def _plan(db, period, noise):
    basis = json.loads(noise['basis']) if noise['basis'] else {}
    windows = basis.get('windows') or []
    devices = {row['device_id']: row['location'] for row in db.execute('SELECT device_id,location FROM acoustic_device')}
    locations = set()
    for row in db.execute('SELECT sensor,device_id FROM noise_sample WHERE period=?', (period,)):
        locations.add(devices.get(row['device_id'], row['sensor']) if row['device_id'] else row['sensor'])
    paths = sorted(location for location in locations if location.startswith('boundary'))
    plan = []
    for window in windows:
        for path in paths:
            plan.append({'kind': 'shutdown', 'window': window, 'path': path,
                         'steps': ['pause outdoor activity for a 10-minute control inside the window',
                                   'record boundary and road-reference Leq before and during the pause',
                                   'log road background and wind on both sides; accept only comparable conditions']})
            plan.append({'kind': 'barrier', 'window': window, 'path': path,
                         'steps': ['erect a temporary barrier on the attributed path',
                                   'compare boundary Leq with and without the barrier',
                                   'log road background and wind on both sides; accept only comparable conditions']})
    return plan


def _valid_check(check, remediation, path):
    if remediation['status'] == 'planned':
        return False, 'measure still planned, not installed or executed'
    if remediation['status'] not in ('installed', 'executed'):
        return False, f"unsupported measure status {remediation['status']}"
    if check['path'] != path:
        return False, f"re-measurement on {check['path']} is not on the attributed path {path}"
    if check['store_db'] > check['measured_db'] + 1e-9:
        return False, 'claimed store dose exceeds the total reading'
    if check['store_db'] > LIMIT:
        return False, f"re-measured store dose {check['store_db']} dB still above limit {LIMIT}"
    return True, 'verified'


def decide_scope(db, period):
    """Remediation acceptance and committable outdoor hours. A commitment needs
    an upheld complaint, a measure that is actually installed/executed (a planned
    barrier does not count) and an on-site re-measurement on the attributed
    store-source path. Each route keeps its own evidence: one route's check
    never substitutes another route's execution."""
    complaint = db.execute('SELECT * FROM complaint_decision WHERE period=?', (period,)).fetchone()
    noise = db.execute('SELECT * FROM noise_decision WHERE period=?', (period,)).fetchone()
    remediations = db.execute('SELECT * FROM remediation WHERE period=? ORDER BY at,event_id', (period,)).fetchall()
    checks = db.execute('SELECT * FROM remediation_check WHERE period=? ORDER BY event_id', (period,)).fetchall()
    by_route, unverified = {}, []
    if complaint and complaint['state'] == 'upheld' and noise and noise['path']:
        by_id = {row['event_id']: row for row in remediations}
        for check in checks:
            remediation = by_id.get(check['remediation_id'])
            if not remediation:
                continue
            ok, why = _valid_check(check, remediation, noise['path'])
            if ok:
                by_route[remediation['route']] = {
                    'route': remediation['route'], 'kind': remediation['kind'],
                    'outdoor_until': remediation['outdoor_until'], 'store_db': check['store_db'],
                    'window': check['window'], 'check': check['event_id'], 'detail': remediation['detail'],
                    'basis': (f"{remediation['status']} measure verified on {check['path']}: "
                              f"re-measured store dose {check['store_db']} dB within limit {LIMIT}")}
            else:
                unverified.append(f"{remediation['route']}: {why}")
        checked_routes = {by_id[check['remediation_id']]['route'] for check in checks if check['remediation_id'] in by_id}
        for remediation in remediations:
            if remediation['route'] not in checked_routes:
                unverified.append(f"{remediation['route']}: no on-site re-measurement")
    commitments = [by_route[route] for route in sorted(by_route)]
    if commitments:
        state, reason = 'committed', 'verified remediation on attributed path; outdoor hours committed'
    elif remediations and complaint and complaint['state'] == 'upheld':
        state, reason = 'unverified', 'remediation not verified on attributed path'
    else:
        state, reason = 'none', 'no upheld complaint requiring remediation commitment'
    if unverified:
        reason += '; unverified: ' + '; '.join(unverified)
    return update_period_decision(db, 'business_scope', period,
                                  dict(state=state, reason=reason,
                                       commitments=json.dumps(commitments, ensure_ascii=False, sort_keys=True)))


def import_data(db, data):
    touched = set()
    with db:
        for value in data.get('complaints', []):
            if record(db, value['event_id'], 'neighbor_complaint', value):
                db.execute('INSERT INTO neighbor_complaint(event_id,period,claimant,address,claim) VALUES(?,?,?,?,?)',
                           (value['event_id'], value['period'], value['claimant'], value['address'], value['claim']))
            touched.add(value['period'])
        return [decide(db, period) for period in sorted(touched)]


def import_remediation(db, data):
    touched = set()
    with db:
        for value in data.get('remediations', []):
            if value['status'] not in ('planned', 'installed', 'executed'):
                raise ValueError('unsupported remediation status')
            if record(db, value['event_id'], 'remediation', value):
                db.execute('INSERT INTO remediation(event_id,period,route,kind,status,at,path,outdoor_until,detail) VALUES(?,?,?,?,?,?,?,?,?)',
                           (value['event_id'], value['period'], value['route'], value['kind'], value['status'],
                            value['at'], value['path'], value.get('outdoor_until'), value['detail']))
            touched.add(value['period'])
        for value in data.get('checks', []):
            remediation = db.execute('SELECT period FROM remediation WHERE event_id=?', (value['remediation_id'],)).fetchone()
            if not remediation or remediation['period'] != value['period']:
                raise ValueError('remediation check must cite existing remediation for same period')
            if record(db, value['event_id'], 'remediation_check', value):
                db.execute('INSERT INTO remediation_check(event_id,period,remediation_id,window,path,measured_db,store_db,background_db,wind) VALUES(?,?,?,?,?,?,?,?,?)',
                           (value['event_id'], value['period'], value['remediation_id'], value['window'], value['path'],
                            value['measured_db'], value['store_db'], value['background_db'], value['wind']))
            touched.add(value['period'])
        return [decide_scope(db, period) for period in sorted(touched)]
