from .store import update_decision


def decide(db, animal_id):
    skill = db.execute('SELECT * FROM skill_decision WHERE animal_id=?', (animal_id,)).fetchone()
    if not skill:
        return None
    credentials = db.execute(
        "SELECT scope,setting,cue_support,handler,protocol_id,score,attempts,reason,issued_at FROM scope_credential "
        "WHERE animal_id=? AND state='issued' ORDER BY scope", (animal_id,)).fetchall()
    blocked = db.execute(
        "SELECT scope,reason FROM scope_credential WHERE animal_id=? AND state='blocked' ORDER BY scope",
        (animal_id,)).fetchall()
    pending_trials = db.execute(
        "SELECT COUNT(*) FROM recall_trial WHERE animal_id=? AND annotation_state='disputed'", (animal_id,)).fetchone()[0]

    if credentials:
        credential = ';'.join(row['scope'] for row in credentials)
        state = 'graduated'
        reason = 'owner scope(s) published only from sealed pre-registered blind tests with raw-clip review and welfare closure'
    else:
        credential = ''
        state = 'practice'
        reason = 'no owner-available recall scope has closed; training success alone never grants off-lead permission'
        if blocked:
            reason += '; risky setting blocked by welfare stop'
        if pending_trials:
            reason += f'; {pending_trials} disputed trial(s) remain pending'

    result = update_decision(db, 'graduation_decision', animal_id,
                             dict(state=state, reason=reason, credential=credential))
    result['scope_credentials'] = [dict(row) for row in credentials]
    result['blocked_scopes'] = [dict(row) for row in blocked]
    return result
