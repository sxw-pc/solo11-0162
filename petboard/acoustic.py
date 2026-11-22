import json
import math
from datetime import datetime, timedelta
from .source import record
from .store import update_period_decision

LIMIT = 65.0
DRIFT_TOLERANCE = 1.0
BACKGROUND_COMPARABILITY_DB = 2.0
WINDOW_MINUTES = 10


def window_of(at):
    """Unified sampling window: every reading lands in the same 10-minute grid."""
    start = datetime.fromisoformat(at)
    start = start.replace(minute=(start.minute // WINDOW_MINUTES) * WINDOW_MINUTES, second=0, microsecond=0)
    end = start + timedelta(minutes=WINDOW_MINUTES)
    return f'{start.isoformat()}/{end.isoformat()}'


def window_seconds(window):
    start, end = window.split('/')
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def _energy(level):
    return 10.0 ** (level / 10.0)


def _level(energy):
    return 10.0 * math.log10(energy)


def leq(pairs):
    """Equivalent continuous level over (db, duration_s) pairs, in energy units."""
    total = sum(duration for _, duration in pairs)
    return _level(sum(_energy(level) * duration for level, duration in pairs) / total)


def energy_subtract(total_db, background_db):
    """Source dose = total minus background, in energy units (never dB minus dB)."""
    diff = _energy(total_db) - _energy(background_db)
    return None if diff <= 0 else _level(diff)


def _devices(db):
    return {row['device_id']: dict(row) for row in db.execute('SELECT * FROM acoustic_device')}


def _windows(db, period, devices):
    """Leq per (location, unified window) at boundary, courtyard and road points."""
    groups = {}
    for row in db.execute('SELECT * FROM noise_sample WHERE period=?', (period,)):
        location = row['sensor']
        calibrated = False
        if row['device_id'] and row['device_id'] in devices:
            device = devices[row['device_id']]
            location = device['location']
            calibrated = device['drift_db'] <= DRIFT_TOLERANCE
        groups.setdefault((location, row['window']), []).append((row['db'], row['duration_s'] or 1.0, calibrated))
    return {key: {'leq': leq([(level, duration) for level, duration, _ in items]),
                  'calibrated': all(ok for _, _, ok in items)}
            for key, items in groups.items()}


def _experiment_dose(exp):
    """A shutdown/barrier control only isolates the store dose when the road
    background and wind on both sides are comparable; a quiet segment recorded
    under different weather is not evidence in either direction."""
    problems = []
    if exp['control_wind'] != exp['baseline_wind']:
        problems.append(f"wind differs ({exp['baseline_wind']}->{exp['control_wind']})")
    if abs(exp['baseline_background_db'] - exp['control_background_db']) > BACKGROUND_COMPARABILITY_DB:
        problems.append(f"road background differs ({exp['baseline_background_db']}->{exp['control_background_db']} dB)")
    if exp['control_db'] > exp['baseline_db']:
        problems.append('control reading louder than baseline')
    if problems:
        return None, '; '.join(problems)
    dose = energy_subtract(exp['baseline_db'], exp['control_db'])
    return (dose if dose is not None else 0.0), 'comparable control'


def _uncertainty(invalid, windows=None):
    notes = [f"experiment {item['event_id']} rejected: {item['why']}" for item in invalid]
    if windows is not None and any(not info['calibrated'] for info in windows.values()):
        notes.append('readings include unregistered or drifted devices')
    return '; '.join(notes) or None


def _passive_window(db, period, location, window, info, windows, devices):
    """Passive separation of one window: synchronized voiceprint clips, courtyard
    propagation check and road background deducted in energy units. Simultaneous
    same-frequency sources (mixed clips) cannot be separated passively."""
    issues = []
    if not info['calibrated']:
        issues.append('readings from unregistered or drifted devices')
    backgrounds = db.execute('SELECT * FROM background_obs WHERE period=? AND window=?', (period, window)).fetchall()
    if not backgrounds:
        issues.append('road background observation missing')
    clips = db.execute('SELECT * FROM audio_clip WHERE period=? AND window=? ORDER BY at,event_id', (period, window)).fetchall()
    if not clips:
        issues.append('synchronized audio clips missing')
    bad = [c['event_id'] for c in clips if c['classification'] in ('mixed', 'unknown')]
    if bad:
        issues.append(f"clips not uniquely classifiable: {', '.join(bad)}")
    drifted = sorted({c['device_id'] for c in clips
                      if c['device_id'] in devices and devices[c['device_id']]['drift_db'] > DRIFT_TOLERANCE})
    if drifted:
        issues.append(f"clip devices beyond calibration tolerance: {', '.join(drifted)}")
    courtyard = windows.get(('courtyard', window))
    if courtyard is None:
        issues.append('courtyard propagation reading missing')
    if issues:
        return None, issues, None
    background = max(row['db'] for row in backgrounds)
    winds = sorted({row['wind'] for row in backgrounds})
    seconds = window_seconds(window)
    non_store = [c for c in clips if c['classification'] != 'dog_bark']
    covered = min(seconds, sum(c['duration_s'] for c in non_store))
    e_non_store = sum(_energy(c['db']) * c['duration_s'] for c in non_store) + _energy(background) * (seconds - covered)
    e_total = _energy(info['leq']) * seconds
    dose = 0.0 if e_total <= e_non_store else _level((e_total - e_non_store) / seconds)
    if courtyard['leq'] < dose - 0.1:
        return None, [f"courtyard Leq {courtyard['leq']:.1f} inconsistent with store dose {dose:.1f} along propagation path"], None
    detail = {'background': background,
              'basis': f"road background {background} dB (wind {'/'.join(winds)}) deducted in energy units over {window}",
              'clips': [c['event_id'] for c in clips]}
    return dose, [], detail


def _window_leq(windows):
    return {key[1]: info['leq'] for key, info in sorted(windows.items()) if key[0].startswith('boundary')}


def decide(db, period):
    samples = db.execute('SELECT * FROM noise_sample WHERE period=? ORDER BY at,event_id', (period,)).fetchall()
    experiments = db.execute('SELECT * FROM isolation_experiment WHERE period=? ORDER BY event_id', (period,)).fetchall()
    if not samples and not experiments:
        return None
    devices = _devices(db)
    boundary_levels = [row['db'] for row in samples
                       if (devices.get(row['device_id'], {}).get('location') or row['sensor']).startswith('boundary')]
    peak = max((boundary_levels or [row['db'] for row in samples]) + [exp['baseline_db'] for exp in experiments])
    windows = _windows(db, period, devices)
    exceeding = {key: info for key, info in windows.items() if key[0].startswith('boundary') and info['leq'] >= LIMIT}
    valid, invalid = [], []
    for exp in experiments:
        dose, why = _experiment_dose(exp)
        if dose is None:
            invalid.append({'event_id': exp['event_id'], 'why': why})
        else:
            valid.append((exp, dose))
    if valid:
        # A comparable isolation experiment is the decisive evidence tier.
        exp, dose = max(valid, key=lambda item: item[1])
        state = 'exceeded' if dose >= LIMIT else 'clear'
        basis = {'method': 'experiment', 'windows': [exp['baseline_window']], 'window_leq': _window_leq(windows),
                 'store_db': dose, 'background_db': exp['control_background_db'],
                 'background_basis': (f"{exp['kind']} control {exp['control_window']}: road background "
                                      f"{exp['baseline_background_db']}->{exp['control_background_db']} dB, "
                                      f"wind {exp['baseline_wind']}->{exp['control_wind']} (comparable)"),
                 'experiments': [item[0]['event_id'] for item in valid], 'invalid_experiments': invalid,
                 'path': exp['path'], 'uncertainty': _uncertainty(invalid)}
        fields = dict(state=state,
                      reason=f"attributable store dose {'exceeds' if state == 'exceeded' else 'within'} boundary limit via controlled {exp['kind']} experiment",
                      peak_db=peak, window=exp['baseline_window'], store_db=dose,
                      background_db=exp['control_background_db'], path=exp['path'],
                      basis=json.dumps(basis, ensure_ascii=False, sort_keys=True), uncertainty=basis['uncertainty'])
    elif exceeding:
        doses, problems = [], []
        for (location, window), info in sorted(exceeding.items()):
            dose, issues, detail = _passive_window(db, period, location, window, info, windows, devices)
            if issues:
                problems.extend(f'{window}@{location}: {issue}' for issue in issues)
            else:
                doses.append((dose, location, window, detail))
        if problems:
            notes = []
            base = _uncertainty(invalid)
            if base:
                notes.append(base)
            notes.extend(problems)
            basis = {'method': 'passive', 'windows': sorted({key[1] for key in exceeding}), 'window_leq': _window_leq(windows),
                     'store_db': None, 'background_db': None, 'background_basis': None, 'experiments': [],
                     'invalid_experiments': invalid, 'path': None, 'unresolved': problems,
                     'uncertainty': '; '.join(notes)}
            fields = dict(state='uncertain', reason='store source contribution not uniquely identifiable',
                          peak_db=peak, window=None, store_db=None, background_db=None, path=None,
                          basis=json.dumps(basis, ensure_ascii=False, sort_keys=True), uncertainty=basis['uncertainty'])
        else:
            dose, location, window, detail = max(doses, key=lambda item: item[0])
            state = 'exceeded' if dose >= LIMIT else 'clear'
            basis = {'method': 'passive', 'windows': [window], 'window_leq': _window_leq(windows), 'store_db': dose,
                     'background_db': detail['background'], 'background_basis': detail['basis'], 'experiments': [],
                     'invalid_experiments': invalid, 'path': location, 'clips': detail['clips'],
                     'uncertainty': _uncertainty(invalid)}
            fields = dict(state=state,
                          reason=f"attributable store dose {'exceeds' if state == 'exceeded' else 'within'} boundary limit after synchronized source separation",
                          peak_db=peak, window=window, store_db=dose, background_db=detail['background'],
                          path=location, basis=json.dumps(basis, ensure_ascii=False, sort_keys=True),
                          uncertainty=basis['uncertainty'])
    elif samples:
        basis = {'method': 'total-bound', 'windows': sorted({key[1] for key in windows if key[0].startswith('boundary')}),
                 'window_leq': _window_leq(windows), 'store_db': None, 'background_db': None,
                 'background_basis': 'none required; total Leq below limit bounds store contribution',
                 'experiments': [], 'invalid_experiments': invalid, 'path': None,
                 'uncertainty': _uncertainty(invalid, windows)}
        fields = dict(state='clear', reason='total Leq below boundary limit bounds store contribution',
                      peak_db=peak, window=None, store_db=None, background_db=None, path=None,
                      basis=json.dumps(basis, ensure_ascii=False, sort_keys=True), uncertainty=basis['uncertainty'])
    else:
        basis = {'method': 'experiment', 'windows': [], 'window_leq': {}, 'store_db': None, 'background_db': None,
                 'background_basis': None, 'experiments': [], 'invalid_experiments': invalid, 'path': None,
                 'uncertainty': _uncertainty(invalid) or 'no boundary readings'}
        fields = dict(state='uncertain', reason='no comparable isolation experiment and no boundary readings',
                      peak_db=peak, window=None, store_db=None, background_db=None, path=None,
                      basis=json.dumps(basis, ensure_ascii=False, sort_keys=True), uncertainty=basis['uncertainty'])
    answer = update_period_decision(db, 'noise_decision', period, fields)
    from .complaint import decide as complaint_decide
    complaint_decide(db, period)
    return answer


def import_data(db, data):
    touched = set()
    with db:
        for value in data.get('devices', []):
            if record(db, value['event_id'], 'acoustic_device', value):
                db.execute('INSERT INTO acoustic_device(event_id,device_id,kind,location,calibrated_at,calibrator,drift_db,bands) VALUES(?,?,?,?,?,?,?,?)',
                           (value['event_id'], value['device_id'], value['kind'], value['location'],
                            value['calibrated_at'], value['calibrator'], value['drift_db'],
                            json.dumps(value.get('bands', []), ensure_ascii=False)))
            # New device identities can resolve previously uncertain windows.
            touched.update(row[0] for row in db.execute('SELECT DISTINCT period FROM noise_sample'))
        for value in data.get('samples', []):
            device_id = value.get('device_id')
            if device_id and not db.execute('SELECT 1 FROM acoustic_device WHERE device_id=?', (device_id,)).fetchone():
                raise ValueError(f'unknown acoustic device: {device_id}')
            if record(db, value['event_id'], 'noise_sample', value):
                db.execute('INSERT INTO noise_sample(event_id,period,sensor,at,db,device_id,window,duration_s,spectrum,sha) VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (value['event_id'], value['period'], value['sensor'], value['at'], value['db'], device_id,
                            window_of(value['at']), value.get('duration_s'),
                            json.dumps(value['spectrum'], ensure_ascii=False) if value.get('spectrum') else None,
                            value.get('sha')))
            touched.add(value['period'])
        for value in data.get('clips', []):
            if not db.execute('SELECT 1 FROM acoustic_device WHERE device_id=?', (value['device_id'],)).fetchone():
                raise ValueError(f"unknown acoustic device: {value['device_id']}")
            if record(db, value['event_id'], 'audio_clip', value):
                db.execute('INSERT INTO audio_clip(event_id,period,device_id,window,at,duration_s,db,classification,spectrum,sha) VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (value['event_id'], value['period'], value['device_id'], window_of(value['at']), value['at'],
                            value['duration_s'], value['db'], value['classification'],
                            json.dumps(value.get('spectrum', {}), ensure_ascii=False), value['sha']))
            touched.add(value['period'])
        for value in data.get('backgrounds', []):
            if record(db, value['event_id'], 'background_obs', value):
                db.execute('INSERT INTO background_obs(event_id,period,source,window,at,db,wind) VALUES(?,?,?,?,?,?,?)',
                           (value['event_id'], value['period'], value['source'], window_of(value['at']),
                            value['at'], value['db'], value['wind']))
            touched.add(value['period'])
        for value in data.get('experiments', []):
            if record(db, value['event_id'], 'isolation_experiment', value):
                db.execute('INSERT INTO isolation_experiment(event_id,period,kind,path,baseline_window,control_window,baseline_db,control_db,baseline_background_db,control_background_db,baseline_wind,control_wind) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           (value['event_id'], value['period'], value['kind'], value['path'], value['baseline_window'],
                            value['control_window'], value['baseline_db'], value['control_db'],
                            value['baseline_background_db'], value['control_background_db'],
                            value['baseline_wind'], value['control_wind']))
            touched.add(value['period'])
        return [decide(db, period) for period in sorted(touched)]
