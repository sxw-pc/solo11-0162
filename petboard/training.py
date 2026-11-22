import hashlib
import json
from pathlib import Path

from .source import record
from .store import update_decision

PASS_STATE = 'passed'
PRACTICE_STATE = 'practice'
CUE_LEVELS = ('none', 'voice', 'gesture', 'voice+gesture')
ANNOTATION_STATES = ('confirmed', 'disputed')
REVIEW_RESULTS = ('valid', 'invalid')

# Adjudication reasons. Pending items stay reviewable and never enter a score.
R_DUPLICATE = 'excluded:burst-frame-not-independent-trial'
R_DISPUTED = 'pending:annotation-dispute'
R_WELFARE_STOP = 'excluded:trial-after-welfare-stop'
R_OFF_CAMERA = 'excluded:off-camera-cue-detected-on-raw-clip'
R_PHYSICAL = 'excluded:physical-guidance-not-recall'
R_REPEATED = 'excluded:repeated-prompt-not-unaided-recall'
R_INCLUDED = 'included:adjudicated-independent-trial'
R_INCLUDED_CUED = 'included:adjudicated-cued-trial'


def _stopped_settings(db, animal_id):
    stopped = {}
    for event in db.execute('SELECT setting, at FROM welfare_event WHERE animal_id=? AND stopped=1', (animal_id,)):
        stopped.setdefault(event['setting'], event['at'])
    return stopped


def adjudicate(trial, stopped_settings):
    """Return per-trial adjudication. Only eligible, non-pending trials may enter a score."""
    reason = R_INCLUDED if trial['cue'] == 'none' else R_INCLUDED_CUED
    if trial['duplicate_of']:
        reason = R_DUPLICATE
    elif trial['annotation_state'] == 'disputed':
        reason = R_DISPUTED
    elif trial['setting'] in stopped_settings and trial['started_at'] >= stopped_settings[trial['setting']]:
        reason = R_WELFARE_STOP
    elif trial['off_camera_cue']:
        reason = R_OFF_CAMERA
    elif trial['physical_guidance']:
        reason = R_PHYSICAL
    elif trial['repeated_prompt']:
        reason = R_REPEATED
    status = reason.split(':', 1)[0]
    eligible = status == 'included'
    return {
        'event_id': trial['event_id'],
        'session_id': trial['session_id'],
        'protocol_id': trial['protocol_id'],
        'setting': trial['setting'],
        'handler': trial['handler'],
        'cue': trial['cue'],
        'arrived': bool(trial['arrived']),
        'status': status,
        'reason': reason,
        'eligible': eligible,
        'independent': eligible and trial['cue'] == 'none',
        'stress_actions': json.loads(trial['stress_actions']),
        'clip_id': trial['clip_id'],
        'window': [trial['window_start_ms'], trial['window_end_ms']],
        'annotator': trial['annotator'],
        'annotation_state': trial['annotation_state'],
    }


def _empty_rate():
    return {'successes': 0, 'attempts': 0, 'rate': None}


def capability_strata(adjudications):
    """Per-setting, per cue-support rates over eligible trials only.

    Rates are intentionally layered: handler/setting transfer is never assumed,
    and cued successes are never pooled into unaided capability.
    """
    strata = {}
    for item in adjudications:
        bucket = strata.setdefault(item['setting'], {
            'independent': _empty_rate(), 'cued': _empty_rate(),
            'excluded': 0, 'pending': 0, 'reasons': [], 'handlers': set()})
        bucket['handlers'].add(item['handler'])
        if item['status'] == 'pending':
            bucket['pending'] += 1
            bucket['reasons'].append({'event_id': item['event_id'], 'reason': item['reason']})
            continue
        if not item['eligible']:
            bucket['excluded'] += 1
            bucket['reasons'].append({'event_id': item['event_id'], 'reason': item['reason']})
            continue
        level = bucket['independent'] if item['independent'] else bucket['cued']
        level['attempts'] += 1
        level['successes'] += 1 if item['arrived'] else 0
    for bucket in strata.values():
        bucket['handlers'] = sorted(bucket['handlers'])
        for level in (bucket['independent'], bucket['cued']):
            level['rate'] = round(level['successes'] / level['attempts'], 4) if level['attempts'] else None
    return strata


def _protocol_trials(db, protocol_id):
    return db.execute('SELECT * FROM recall_trial WHERE protocol_id=? ORDER BY started_at,event_id', (protocol_id,)).fetchall()


def evaluate_protocol(db, protocol, adjudications, stopped_settings):
    """Sealed-protocol closure check. A layered success rate alone can never pass this."""
    pid = protocol['protocol_id']
    by_id = {item['event_id']: item for item in adjudications if item['protocol_id'] == pid}
    trials = _protocol_trials(db, pid)
    review = db.execute('SELECT * FROM blind_review WHERE protocol_id=? ORDER BY reviewed_at,event_id', (pid,)).fetchone()
    allowed = json.loads(protocol['allowed_cues'])
    failures = []
    if not protocol['sealed']:
        failures.append('protocol was not sealed before testing began')
    if not protocol['randomized']:
        failures.append('cue/position order was not randomized; fixed-order leakage cannot be ruled out')
    if not protocol['independent_observer'] or protocol['independent_observer'] == protocol['tested_owner']:
        failures.append('independent observer missing or identical to tested owner')
    if not trials:
        failures.append('no recall trials linked to protocol')
    if any(t['started_at'] < protocol['registered_at'] for t in trials):
        failures.append('trials predate protocol registration; protocol was not pre-registered')
    if protocol['independent_observer'] and any(t['handler'] == protocol['independent_observer'] for t in trials):
        failures.append('observer also handled the animal and is not independent')
    if not review:
        failures.append('no post-test raw-clip review record')
    elif review['result'] != 'valid':
        failures.append('raw-clip review invalidated the test')
    deviations = set(json.loads(review['deviations'])) if review else set()
    if review and review['off_camera_found'] != 'none' and review['off_camera_found'] not in deviations:
        failures.append(f"off-camera cue {review['off_camera_found']} not covered by a signed review deviation")
    if review and review['duplicates_found'] != 'none':
        for dup in str(review['duplicates_found']).split(','):
            if dup.strip() and dup.strip() not in deviations:
                failures.append(f"duplicate trial {dup.strip()} not covered by a signed review deviation")
    if any(t['handler'] != protocol['tested_owner'] for t in trials):
        failures.append('blind test must be executed by the registered tested owner')
    if any(t['setting'] != protocol['setting'] for t in trials):
        failures.append('trials drifted outside the registered setting; settings do not transfer by default')
    for trial in trials:
        if trial['setting'] in stopped_settings and trial['started_at'] >= stopped_settings[trial['setting']]:
            failures.append('welfare stop boundary reached in this setting; further blind testing is forbidden')
            break
    independent_scope = protocol['allowed_cues'] == '["none"]'
    for trial in trials:
        if independent_scope and trial['cue'] != 'none' and trial['event_id'] not in deviations:
            failures.append(f"unaided scope protocol contains an undocumented prompted trial {trial['event_id']}")
        elif not independent_scope and trial['cue'] not in allowed and trial['event_id'] not in deviations:
            failures.append(f"cue used in trial {trial['event_id']} is outside the pre-approved cue list and not a signed deviation")
    eligible = [by_id[t['event_id']] for t in trials if t['event_id'] in by_id and by_id[t['event_id']]['eligible']]
    pending = [t for t in trials if t['event_id'] in by_id and by_id[t['event_id']]['status'] == 'pending']
    if pending:
        failures.append('annotation dispute left pending; scope cannot be published yet')
    # Every adjudicated exclusion inside a blind test must be signed off in the raw-clip review;
    # hidden exclusions would silently shrink the sample.
    unexplained_exclusions = [
        t['event_id'] for t in trials
        if t['event_id'] in by_id and not by_id[t['event_id']]['eligible']
        and by_id[t['event_id']]['status'] != 'pending' and t['event_id'] not in deviations]
    if unexplained_exclusions:
        failures.append('excluded trials without signed review deviation: ' + ','.join(unexplained_exclusions))
    attempts = len(eligible)
    successes = sum(1 for item in eligible if item['arrived'])
    rate = round(successes / attempts, 4) if attempts else 0.0
    if attempts < protocol['min_attempts']:
        failures.append(f'only {attempts} eligible independent attempts, minimum is {protocol["min_attempts"]}')
    if attempts and rate < protocol['pass_threshold']:
        failures.append(f'eligible pass rate {rate} below threshold {protocol["pass_threshold"]}')
    cue_support = 'independent' if independent_scope else 'cued'
    scope = f'{cue_support}-recall:{protocol["setting"]}'
    return {
        'protocol_id': pid, 'scope': scope, 'setting': protocol['setting'],
        'cue_support': cue_support, 'handler': protocol['tested_owner'],
        'eligible_attempts': attempts, 'successes': successes, 'rate': rate,
        'valid': not failures, 'failures': failures,
        'issued_at': review['reviewed_at'] if review else protocol['registered_at'],
    }


def decide(db, animal_id):
    animal = db.execute('SELECT 1 FROM animal WHERE id=?', (animal_id,)).fetchone()
    if not animal:
        return None
    trials = db.execute('SELECT * FROM recall_trial WHERE animal_id=? ORDER BY started_at,event_id', (animal_id,)).fetchall()
    stopped = _stopped_settings(db, animal_id)
    adjudications = [adjudicate(t, stopped) for t in trials]
    strata = capability_strata(adjudications)
    protocols = db.execute('SELECT * FROM blind_protocol WHERE animal_id=? ORDER BY registered_at,protocol_id', (animal_id,)).fetchall()
    evaluations = [evaluate_protocol(db, p, adjudications, stopped) for p in protocols]

    db.execute('DELETE FROM scope_credential WHERE animal_id=?', (animal_id,))
    issued, blocked_scopes = [], set()
    for evaluation in evaluations:
        if evaluation['valid']:
            row = dict(animal_id=animal_id, scope=evaluation['scope'], setting=evaluation['setting'],
                       cue_support=evaluation['cue_support'], handler=evaluation['handler'], state='issued',
                       protocol_id=evaluation['protocol_id'], score=evaluation['rate'],
                       attempts=evaluation['eligible_attempts'],
                       reason='sealed pre-registered blind test passed on raw-clip reviewed independent trials',
                       issued_at=evaluation['issued_at'], version=1)
            db.execute('INSERT INTO scope_credential(animal_id,scope,setting,cue_support,handler,state,protocol_id,score,attempts,reason,issued_at,version) VALUES(:animal_id,:scope,:setting,:cue_support,:handler,:state,:protocol_id,:score,:attempts,:reason,:issued_at,:version)', row)
            issued.append(row)
    # Welfare closure: a stopped setting can never publish an owner scope, no matter the rate.
    for setting, at in sorted(stopped.items()):
        for cue_support in ('independent', 'cued'):
            scope = f'{cue_support}-recall:{setting}'
            blocked_scopes.add(scope)
            db.execute('INSERT INTO scope_credential(animal_id,scope,setting,cue_support,handler,state,protocol_id,score,attempts,reason,issued_at,version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (animal_id, scope, setting, cue_support, '', 'blocked', '', 0.0, 0,
                        f'welfare stop recorded at {at}; chasing or sustained stress forbids further blind tests in this setting', at, 1))

    pending_count = sum(1 for item in adjudications if item['status'] == 'pending')
    if issued:
        state = PASS_STATE
        reason = f'{len(issued)} sealed blind-test scope credential(s) issued; {pending_count} trial(s) still pending review'
    elif stopped:
        state = PRACTICE_STATE
        reason = 'no sealed credential issued: welfare stop blocks the risky setting and no other protocol closed'
    elif protocols:
        state = PRACTICE_STATE
        reason = 'no sealed credential issued: registered blind protocols failed closure (see protocol evaluations)'
    elif trials:
        state = PRACTICE_STATE
        reason = 'training trials captured but no pre-registered sealed blind protocol exists'
    else:
        state = PRACTICE_STATE
        reason = 'no recall evidence captured'
    if pending_count and not issued:
        reason += f'; {pending_count} annotation dispute(s) held pending and cannot mask unaided failure'

    if issued:
        score = max(row['score'] for row in issued)
    else:
        coarse = db.execute('SELECT * FROM training_trial WHERE animal_id=? ORDER BY at DESC,event_id DESC LIMIT 1', (animal_id,)).fetchone()
        # A legacy coarse session is descriptive only: its ratio can never publish a scope by itself.
        score = (coarse['successes'] / coarse['attempts']) if coarse and coarse['attempts'] else 0.0
    evidence = db.execute('SELECT event_id FROM recall_trial WHERE animal_id=? UNION SELECT protocol_id FROM blind_protocol WHERE animal_id=? UNION SELECT event_id FROM welfare_event WHERE animal_id=? ORDER BY event_id DESC LIMIT 1',
                          (animal_id, animal_id, animal_id)).fetchone()
    result = update_decision(db, 'skill_decision', animal_id,
                             dict(state=state, reason=reason, skill='recall', score=score),
                             evidence['event_id'] if evidence else None)
    from .graduation import decide as graduation_decide
    graduation_decide(db, animal_id)
    return {
        'skill_decision': result,
        'trial_adjudication': adjudications,
        'capability_by_setting': strata,
        'protocol_evaluations': evaluations,
        'welfare_blocked_settings': sorted(stopped),
        'credentials': [dict(row) for row in db.execute('SELECT * FROM scope_credential WHERE animal_id=? ORDER BY scope', (animal_id,))],
    }


def verify_clips(db, clip_root, animal_id=None):
    """Re-hash local clip files so the index fixture stays replayable against raw footage."""
    root = Path(clip_root)
    sql = 'SELECT * FROM video_clip'
    params = ()
    if animal_id:
        sql += ' WHERE clip_id IN (SELECT DISTINCT clip_id FROM recall_trial WHERE animal_id=?)'
        params = (animal_id,)
    output = []
    for clip in db.execute(sql, params):
        target = root / clip['path']
        if not target.is_file():
            status, detail = 'missing', 'local clip file not found'
        else:
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if digest == clip['sha256']:
                status, detail = 'matched', 'sha256 matches indexed raw clip'
            else:
                status, detail = 'hash-mismatch', 'raw clip changed after annotation was sealed'
        output.append({'clip_id': clip['clip_id'], 'path': clip['path'], 'status': status, 'detail': detail})
    return output


def _insert_clip(db, value):
    if not record(db, value['clip_id'], 'video_clip', value):
        return
    db.execute('INSERT INTO video_clip(clip_id,path,sha256,recorded_at,duration_ms,frame_count,rfid_log,location_log,manifest) VALUES(?,?,?,?,?,?,?,?,?)',
               (value['clip_id'], value['path'], value['sha256'], value['recorded_at'], value['duration_ms'],
                value['frame_count'], json.dumps(value.get('rfid_log', []), ensure_ascii=False),
                json.dumps(value.get('location_log', []), ensure_ascii=False),
                json.dumps(value['manifest'], ensure_ascii=False)))


def _insert_recall_trial(db, value):
    clip = db.execute('SELECT 1 FROM video_clip WHERE clip_id=?', (value['clip_id'],)).fetchone()
    if not clip:
        raise ValueError(f"recall trial {value['event_id']} references unknown clip {value['clip_id']}")
    if value['cue'] not in CUE_LEVELS:
        raise ValueError(f"unsupported cue level: {value['cue']}")
    state = value.get('annotation_state', 'confirmed')
    if state not in ANNOTATION_STATES:
        raise ValueError(f'unsupported annotation state: {state}')
    if value['window_end_ms'] <= value['window_start_ms']:
        raise ValueError('reviewable time window must have positive length')
    if value.get('duplicate_of') and value['duplicate_of'] == value['event_id']:
        raise ValueError('a trial cannot be a duplicate of itself')
    if not record(db, value['event_id'], 'recall_trial', value):
        return
    db.execute('INSERT INTO recall_trial(event_id,animal_id,clip_id,session_id,protocol_id,started_at,ended_at,window_start_ms,window_end_ms,handler,cue,setting,distraction,off_camera_cue,physical_guidance,repeated_prompt,arrived,stress_actions,annotator,annotation_state,note,burst_id,duplicate_of) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (value['event_id'], value['animal_id'], value['clip_id'], value['session_id'], value.get('protocol_id'),
                value['started_at'], value['ended_at'], value['window_start_ms'], value['window_end_ms'],
                value['handler'], value['cue'], value['setting'], value['distraction'],
                int(value.get('off_camera_cue', False)), int(value.get('physical_guidance', False)),
                int(value.get('repeated_prompt', False)), int(value['arrived']),
                json.dumps(value.get('stress_actions', []), ensure_ascii=False), value['annotator'], state,
                value.get('note'), value.get('burst_id'), value.get('duplicate_of')))


def _insert_protocol(db, value):
    if not record(db, value['protocol_id'], 'blind_protocol', value):
        return
    db.execute('INSERT INTO blind_protocol(protocol_id,animal_id,registered_at,target_scope,setting,tested_owner,allowed_cues,randomized,independent_observer,min_attempts,pass_threshold,welfare_stop_conditions,sealed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (value['protocol_id'], value['animal_id'], value['registered_at'], value['target_scope'], value['setting'],
                value['tested_owner'], json.dumps(value['allowed_cues'], ensure_ascii=False), int(value.get('randomized', False)),
                value['independent_observer'], value['min_attempts'], value['pass_threshold'],
                json.dumps(value.get('welfare_stop_conditions', []), ensure_ascii=False), int(value.get('sealed', False))))


def _insert_review(db, value):
    protocol = db.execute('SELECT 1 FROM blind_protocol WHERE protocol_id=?', (value['protocol_id'],)).fetchone()
    if not protocol:
        raise ValueError(f"review {value['event_id']} references unknown protocol {value['protocol_id']}")
    if value['result'] not in REVIEW_RESULTS:
        raise ValueError(f"unsupported review result: {value['result']}")
    if not record(db, value['event_id'], 'blind_review', value):
        return
    db.execute('INSERT INTO blind_review(event_id,protocol_id,animal_id,reviewed_at,reviewer,off_camera_found,duplicates_found,deviations,result,note) VALUES(?,?,?,?,?,?,?,?,?,?)',
               (value['event_id'], value['protocol_id'], value['animal_id'], value['reviewed_at'], value['reviewer'],
                value.get('off_camera_found', 'none'), value.get('duplicates_found', 'none'),
                json.dumps(value.get('deviations', []), ensure_ascii=False), value['result'], value.get('note', '')))


def _insert_welfare_event(db, value):
    if not record(db, value['event_id'], 'welfare_event', value):
        return
    db.execute('INSERT INTO welfare_event(event_id,animal_id,setting,session_id,protocol_id,at,kind,actor,detail,stopped) VALUES(?,?,?,?,?,?,?,?,?,?)',
               (value['event_id'], value['animal_id'], value['setting'], value.get('session_id'), value.get('protocol_id'),
                value['at'], value['kind'], value['actor'], value['detail'], int(value.get('stopped', False))))


def import_data(db, data):
    touched = set()
    with db:
        for value in data.get('clips', []):
            _insert_clip(db, value)
        for value in data.get('trials', []):  # legacy coarse session import, retained
            if record(db, value['event_id'], 'training_trial', value):
                db.execute('INSERT INTO training_trial(event_id,animal_id,at,skill,setting,handler,successes,attempts,video) VALUES(?,?,?,?,?,?,?,?,?)',
                           (value['event_id'], value['animal_id'], value['at'], value['skill'], value['setting'], value['handler'],
                            value['successes'], value['attempts'], value['video']))
            touched.add(value['animal_id'])
        for value in data.get('recall_trials', []):
            _insert_recall_trial(db, value)
            touched.add(value['animal_id'])
        for value in data.get('protocols', []):
            _insert_protocol(db, value)
            touched.add(value['animal_id'])
        for value in data.get('reviews', []):
            _insert_review(db, value)
            touched.add(value['animal_id'])
        for value in data.get('welfare_events', []):
            _insert_welfare_event(db, value)
            touched.add(value['animal_id'])
        return [decide(db, animal_id) for animal_id in sorted(touched)]
