import json
from .source import record
from .store import update_decision

# Chain statuses (anomaly statuses freeze the related service fee).
PENDING_PARENT = 'pending_parent'
CYCLIC = 'cyclic'
CROSS_PLAN = 'cross_plan'
FORK_PENDING = 'fork_pending'
CONFLICTED_ARBITRATION = 'conflicted_arbitration'
INVALID_ARBITRATION = 'invalid_arbitration'
ADOPTED_TIP = 'adopted_tip'
INVALIDATED_ROOT = 'invalidated_root'
SINGLE_ACTIVE = 'single_active'

ANOMALY_STATUSES = {PENDING_PARENT, CYCLIC, CROSS_PLAN, FORK_PENDING,
                    CONFLICTED_ARBITRATION, INVALID_ARBITRATION}


def _load_rows(db, animal_id, plan_id):
    revoked = {r['correction_id'] for r in db.execute(
        'SELECT correction_id FROM correction_revocation WHERE animal_id=?', (animal_id,))}
    rows = {r['event_id']: dict(r) for r in db.execute(
        'SELECT * FROM care_action WHERE animal_id=? AND plan_id=?', (animal_id, plan_id))}
    return rows, revoked


def _build_components(rows, revoked):
    """Group the correction graph of one plan into components of node keys.

    A node key is an event_id present in care_action, or a synthetic
    ``missing:<event_id>`` key when a correction cites evidence that has not
    arrived yet (or was never ingested for this plan).
    """
    parent = {}

    def find(key):
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(a, b):
        parent[find(a)] = find(b)

    for event_id in rows:
        find(event_id)
    edges = []
    for event_id, row in rows.items():
        target = row['supersedes']
        if not target:
            continue
        parent_node = rows.get(target)
        if parent_node is None:
            key = f'missing:{target}'
        else:
            key = target
        union(event_id, key)
        edges.append((event_id, key, target, parent_node))

    groups = {}
    for key in list(parent):
        groups.setdefault(find(key), set()).add(key)
    return list(groups.values()), edges


def _adjudicate(db, animal_id, plan_id):
    """Return the per-root-chain adjudication ledger for one plan version."""
    rows, revoked = _load_rows(db, animal_id, plan_id)
    components, edges = _build_components(rows, revoked)
    all_actions = {r['event_id']: dict(r) for r in db.execute(
        'SELECT event_id,animal_id,plan_id,kind FROM care_action WHERE animal_id=?', (animal_id,))}
    animal_row = db.execute('SELECT owner FROM animal WHERE id=?', (animal_id,)).fetchone()
    owner = animal_row['owner'] if animal_row else None
    arbitrations = [dict(r) for r in db.execute(
        'SELECT * FROM arbitration_evidence WHERE animal_id=? ORDER BY event_id', (animal_id,))]
    arb_revoked = {r['arbitration_id'] for r in db.execute(
        'SELECT arbitration_id FROM arbitration_revocation WHERE animal_id=?', (animal_id,))}

    ledgers = []
    for nodes in sorted(components, key=lambda part: sorted(part)):
        node_set = set(nodes)
        comp_edges = [(c, p, target, parent_row) for c, p, target, parent_row in edges if c in node_set]
        present = [key for key in node_set if not key.startswith('missing:')]
        kinds = {rows[key]['kind'] for key in present}
        kind = sorted(kinds)[0] if kinds else ''

        # Cycle detection: follow supersedes links within this component.
        cyclic = False
        for start in present:
            path = set()
            current = start
            while current in rows and current in node_set:
                if current in path:
                    cyclic = True
                    break
                path.add(current)
                target = rows[current]['supersedes']
                if not target or target not in node_set or target not in rows:
                    break
                current = target
            if cyclic:
                break

        cross_plan = False
        for child, _parent, target, parent_row in comp_edges:
            if parent_row is None:
                global_row = all_actions.get(target)
                if global_row is not None and (global_row['plan_id'] != plan_id
                                                or global_row['kind'] != rows[child]['kind']):
                    cross_plan = True
            elif parent_row['plan_id'] != plan_id or parent_row['kind'] != rows[child]['kind']:
                cross_plan = True

        pending_parent = any(key.startswith('missing:') for key in node_set)
        roots = sorted(key for key in node_set if not key.startswith('missing:')
                       and (not rows[key]['supersedes'] or rows[key]['supersedes'] not in node_set
                            or rows[key]['supersedes'] not in rows))
        root_id = roots[0] if roots else None

        active = sorted(key for key in node_set if not key.startswith('missing:')
                        and key not in revoked)
        # A node is only a live tip when nothing active supersedes it.
        superseded = {parent for child, parent, _t, _pr in comp_edges
                      if parent in rows and child not in revoked and parent not in revoked}
        tips = sorted(key for key in active if key not in superseded)

        node_records = []
        for key in sorted(node_set):
            if key.startswith('missing:'):
                node_records.append({'event_id': key[len('missing:'):], 'present': False,
                                     'excluded_reason': 'parent evidence not yet arrived', 'active': False})
                continue
            row = rows[key]
            reasons = []
            if key in revoked:
                reasons.append('correction withdrawn by revocation')
            if key in superseded:
                reasons.append('superseded by active correction')
            node_records.append({'event_id': key, 'present': True, 'kind': row['kind'],
                                 'supersedes': row['supersedes'], 'witness': row['witness'],
                                 'actor': row['actor'], 'food': row['food'], 'medicine': row['medicine'],
                                 'active': key in tips,
                                 'excluded_reason': '; '.join(reasons) if reasons else None})

        status = SINGLE_ACTIVE
        reason = 'single active chain tip'
        adopted_tip = None
        arbitration_id = None

        if cyclic:
            status, reason = CYCLIC, 'correction chain contains a cycle'
        elif cross_plan:
            status, reason = CROSS_PLAN, 'correction crosses plan or action kind boundary'
        elif pending_parent:
            status, reason = PENDING_PARENT, 'parent evidence has not arrived; meal cannot be counted yet'
        else:
            # Independent arbitration can resolve a fork, confirm a single tip or
            # rule the original action invalid; it never follows id/import order.
            valid = []
            invalid = []
            for arb in arbitrations:
                if arb['event_id'] in arb_revoked or arb['root_id'] != root_id:
                    continue
                if arb['decision'] == 'invalid':
                    ok = (owner is not None and arb['signed_by'] == owner)
                    why = 'owner signed root invalidation' if ok else 'invalidation requires owner signature'
                else:
                    tip_row = rows.get(arb['tip_id'])
                    if tip_row is None or arb['tip_id'] not in tips:
                        ok, why = False, 'arbitration does not select an active tip of this chain'
                    elif arb['decision'] == 'owner_ack':
                        ok = (owner is not None and arb['signed_by'] == owner)
                        why = 'owner acknowledgement of effective tip' if ok else 'owner acknowledgement must be signed by owner'
                    elif arb['decision'] == 'panel_ruling':
                        ok = (bool(arb['witness']) and arb['witness'] != arb['signed_by']
                              and arb['witness'] != tip_row['actor']
                              and (not tip_row['witness'] or arb['witness'] != tip_row['witness'])
                              and arb['signed_by'] != tip_row['actor']
                              and (not tip_row['witness'] or arb['signed_by'] != tip_row['witness']))
                        why = ('two independent care witnesses confirm the true tip' if ok
                               else 'panel ruling needs a witness independent of signer and tip actors')
                    else:
                        ok, why = False, 'unsupported arbitration decision'
                (valid if ok else invalid).append((arb, why))
            selections = {a['event_id']: a for a, _w in valid if a['decision'] != 'invalid'}
            invalidations = [a for a, _w in valid if a['decision'] == 'invalid']
            selected_tips = {a['tip_id'] for a in selections.values()}
            if invalid and not valid:
                status, reason = INVALID_ARBITRATION, '; '.join(sorted({w for _a, w in invalid}))
            elif (len(selected_tips) > 1 or (selected_tips and invalidations)
                  or (len(invalidations) > 1)):
                status, reason = CONFLICTED_ARBITRATION, 'mutually exclusive arbitration evidence remains open'
            elif invalidations:
                chosen = invalidations[0]
                status, reason, arbitration_id = INVALIDATED_ROOT, 'arbitration ruled the original action invalid', chosen['event_id']
            elif selections:
                chosen = next(iter(selections.values()))
                why = next(w for a, w in valid if a['event_id'] == chosen['event_id'])
                status, reason, adopted_tip, arbitration_id = ADOPTED_TIP, why, chosen['tip_id'], chosen['event_id']
            elif len(tips) > 1:
                status, reason = FORK_PENDING, 'parallel witnessed corrections await independent arbitration; service fee frozen'
            elif tips:
                adopted_tip = tips[0]

        # Reflect the adjudication on each node: only the adopted tip stays active;
        # rival tips and an invalidated root carry an explicit exclusion reason.
        if status in (ADOPTED_TIP, INVALIDATED_ROOT):
            for record in node_records:
                if not record['present']:
                    continue
                if status == ADOPTED_TIP and record['event_id'] == adopted_tip:
                    continue
                if record['event_id'] in tips or (status == INVALIDATED_ROOT and record['event_id'] == root_id):
                    record['active'] = False
                    note = ('rejected by arbitration in favour of ' + adopted_tip
                            if status == ADOPTED_TIP else 'ruled invalid by arbitration')
                    record['excluded_reason'] = '; '.join(filter(None, [record['excluded_reason'], note]))

        ledgers.append({'root_id': root_id, 'plan_id': plan_id, 'kind': kind,
                        'status': status, 'reason': reason, 'adopted_tip_id': adopted_tip,
                        'arbitration_id': arbitration_id, 'tips': tips, 'nodes': node_records})
    return ledgers


def decide(db, animal_id):
    intake = db.execute('SELECT * FROM intake_decision WHERE animal_id=?', (animal_id,)).fetchone()
    plans = db.execute('SELECT * FROM care_plan WHERE animal_id=? ORDER BY version DESC,event_id', (animal_id,)).fetchall()
    if not plans:
        state, reason, missed, compensation = 'pending', 'care plan missing', 0, 0
        ledgers = []
    else:
        plan = plans[0]
        parallels = [r for r in plans if r['version'] == plan['version']]
        certificate = db.execute('SELECT * FROM health_certificate WHERE event_id=?', (intake['certificate_id'],)).fetchone() if intake and intake['certificate_id'] else None
        forbidden = set(json.loads(certificate['contraindications'])) if certificate else set()
        ledgers = _adjudicate(db, animal_id, plan['event_id'])
        anomaly = next((item for item in ledgers if item['status'] in ANOMALY_STATUSES), None)
        animal = db.execute('SELECT * FROM animal WHERE id=?', (animal_id,)).fetchone()

        def tip_row(item):
            if item['adopted_tip_id']:
                return db.execute('SELECT * FROM care_action WHERE event_id=?', (item['adopted_tip_id'],)).fetchone()
            return None

        meal_ledgers = [item for item in ledgers if item['kind'] == 'meal']
        med_ledgers = [item for item in ledgers if item['kind'] == 'medicine']
        medicines = [row for row in (tip_row(item) for item in med_ledgers) if row is not None]

        if anomaly:
            # Frozen lineage: never count two tips as two meals and never settle credit.
            missed, compensation = 0, 0
        else:
            # Every non-invalidated meal chain is one served slot (its adopted tip);
            # an invalidated root means the original meal never boundedly happened.
            # A tip without an independent witness cannot prove the meal was served.
            served_chains = []
            food_mismatch = False
            for item in meal_ledgers:
                if item['status'] == INVALIDATED_ROOT:
                    continue
                tip = tip_row(item)
                if tip is not None and tip['witness'] and tip['witness'] != tip['actor']:
                    served_chains.append(item)
                    if tip['food'] != plan['food']:
                        food_mismatch = True
            missed = max(0, plan['meals'] - len(served_chains))
            if not missed and food_mismatch:
                # A served but non-approved-recipe meal still earns service-fee compensation.
                missed = plan['meals']
            compensation = missed * animal['service_fee'] if animal else 0

        if not intake or intake['state'] != 'accepted':
            state, reason = 'blocked', 'animal health intake not accepted'
        elif len({(r['food'], r['medicine'], r['meals']) for r in parallels}) > 1:
            state, reason = 'frozen', 'parallel plan version conflict'
        elif plan['approved_by'] != 'owner':
            state, reason = 'blocked', 'owner has not approved plan'
        elif plan['food'] in forbidden or (plan['medicine'] and plan['medicine'] in forbidden):
            state, reason = 'blocked', 'plan contains veterinarian contraindication'
        elif anomaly:
            state = 'frozen' if anomaly['status'] in (PENDING_PARENT, FORK_PENDING) else 'disputed'
            reason = anomaly['reason']
        elif any(r['medicine'] != plan['medicine'] or not r['witness'] or r['witness'] == r['actor'] for r in medicines):
            state, reason = 'disputed', 'medicine identity or independent witness missing'
        elif plan['medicine'] and not medicines:
            state, reason = 'partial', 'prescribed medicine has no witnessed action'
        elif any(item['status'] == INVALIDATED_ROOT or (tip_row(item) is not None and tip_row(item)['food'] != plan['food'])
                 for item in meal_ledgers):
            state, reason = 'partial', 'adjudicated meal differs from approved plan or was ruled invalid'
        elif missed:
            state, reason = 'partial', 'missed meal compensation due'
        else:
            state, reason = 'fulfilled', 'approved plan and witnessed care fulfilled'

    previous = db.execute('SELECT * FROM care_decision WHERE animal_id=?', (animal_id,)).fetchone()
    result = update_decision(db, 'care_decision', animal_id,
                             dict(state=state, reason=reason, missed_meals=missed, compensation=compensation))
    # Append a lineage snapshot when the decision text changed or the chain
    # lineage itself changed (e.g. a late revocation swaps the active tip while
    # the overall care state stays 'fulfilled'). Idempotent evidence re-imports
    # produce neither change, so replay stays byte-identical.
    lineage_signature = json.dumps(
        [(item['root_id'], item['status'], item['adopted_tip_id'], item['arbitration_id'],
          [(n['event_id'], n['active'], n['excluded_reason']) for n in item['nodes']])
         for item in ledgers], ensure_ascii=False, sort_keys=True)
    decision_changed = not previous or any(
        previous[name] != result[name] for name in ('state', 'reason', 'missed_meals', 'compensation'))
    lineage_changed = True
    if db.execute('SELECT 1 FROM care_chain_ledger WHERE animal_id=? LIMIT 1', (animal_id,)).fetchone():
        prior_rows = db.execute(
            'SELECT root_id,status,adopted_tip_id,arbitration_id,nodes FROM care_chain_ledger WHERE animal_id=? AND id IN (SELECT MAX(id) FROM care_chain_ledger WHERE animal_id=? GROUP BY root_id) ORDER BY root_id',
            (animal_id, animal_id)).fetchall()
        prior_sig = json.dumps(
            [(r['root_id'], r['status'], r['adopted_tip_id'], r['arbitration_id'],
              [(n['event_id'], n['active'], n['excluded_reason']) for n in json.loads(r['nodes'])])
             for r in prior_rows], ensure_ascii=False, sort_keys=True)
        lineage_changed = prior_sig != lineage_signature
    if decision_changed or lineage_changed:
        _write_ledger(db, animal_id, result['version'], ledgers)
    from .handoff import decide as handoff_decide
    handoff_decide(db, animal_id)
    return result


def _write_ledger(db, animal_id, decision_version, ledgers):
    for item in ledgers:
        db.execute('INSERT INTO care_chain_ledger(animal_id,root_id,plan_id,kind,decision_version,status,adopted_tip_id,arbitration_id,nodes,reason) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (animal_id, item['root_id'], item['plan_id'], item['kind'], decision_version,
                    item['status'], item['adopted_tip_id'], item['arbitration_id'],
                    json.dumps(item['nodes'], ensure_ascii=False, sort_keys=True), item['reason']))


def import_data(db, data):
    touched = set()
    with db:
        for value in data.get('plans', []):
            if record(db, value['event_id'], 'plan', value):
                db.execute('INSERT INTO care_plan(event_id,animal_id,version,food,medicine,meals,approved_by) VALUES(?,?,?,?,?,?,?)',
                           (value['event_id'], value['animal_id'], value['version'], value['food'], value.get('medicine', ''), value['meals'], value['approved_by']))
            touched.add(value['animal_id'])
        for value in data.get('actions', []):
            if value.get('supersedes'):
                # Reference validity is enforced whenever the parent evidence is already
                # present; a missing parent is allowed to arrive later (pending_parent).
                old = db.execute('SELECT animal_id,plan_id,kind FROM care_action WHERE event_id=?', (value['supersedes'],)).fetchone()
                if old and (old['animal_id'], old['plan_id'], old['kind']) != (value['animal_id'], value['plan_id'], value['kind']):
                    raise ValueError('correction must supersede a matching action of the same animal, plan and kind')
            if record(db, value['event_id'], 'action', value):
                db.execute('INSERT INTO care_action(event_id,animal_id,plan_id,kind,day,actor,witness,food,medicine,supersedes) VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (value['event_id'], value['animal_id'], value['plan_id'], value['kind'], value['day'], value['actor'], value.get('witness'), value.get('food'), value.get('medicine'), value.get('supersedes')))
            touched.add(value['animal_id'])
        for value in data.get('correction_revocations', []):
            # The correction itself may still be in flight; a dangling revocation
            # becomes effective when the correction evidence arrives.
            correction = db.execute('SELECT animal_id FROM care_action WHERE event_id=?', (value['correction_id'],)).fetchone()
            if correction and correction['animal_id'] != value['animal_id']:
                raise ValueError('revocation must identify an existing correction for the same animal')
            if record(db, value['event_id'], 'correction_revocation', value):
                db.execute('INSERT INTO correction_revocation(event_id,correction_id,animal_id,at,signed_by,reason) VALUES(?,?,?,?,?,?)',
                           (value['event_id'], value['correction_id'], value['animal_id'], value['at'], value['signed_by'], value['reason']))
            touched.add(value['animal_id'])
        return [decide(db, animal_id) for animal_id in sorted(touched)]


def import_arbitration(db, data):
    touched = set()
    with db:
        for value in data.get('arbitrations', []):
            if value['decision'] not in ('owner_ack', 'panel_ruling', 'invalid'):
                raise ValueError('unsupported arbitration decision')
            if value['decision'] != 'invalid' and not value.get('tip_id'):
                raise ValueError('tip arbitration must cite the selected tip')
            if record(db, value['event_id'], 'arbitration', value):
                db.execute('INSERT INTO arbitration_evidence(event_id,animal_id,root_id,decision,tip_id,signed_by,witness,at,rationale) VALUES(?,?,?,?,?,?,?,?,?)',
                           (value['event_id'], value['animal_id'], value['root_id'], value['decision'],
                            value.get('tip_id'), value['signed_by'], value.get('witness'), value['at'], value.get('rationale', '')))
            touched.add(value['animal_id'])
        for value in data.get('arbitration_revocations', []):
            arb = db.execute('SELECT animal_id,signed_by FROM arbitration_evidence WHERE event_id=?', (value['arbitration_id'],)).fetchone()
            if not arb or arb['animal_id'] != value['animal_id']:
                raise ValueError('arbitration revocation must identify existing evidence for the same animal')
            if value['signed_by'] != arb['signed_by']:
                raise ValueError('arbitration can only be revoked by its original signer')
            if record(db, value['event_id'], 'arbitration_revocation', value):
                db.execute('INSERT INTO arbitration_revocation(event_id,arbitration_id,animal_id,at,signed_by,reason) VALUES(?,?,?,?,?,?)',
                           (value['event_id'], value['arbitration_id'], value['animal_id'], value['at'], value['signed_by'], value['reason']))
            touched.add(value['animal_id'])
        return [decide(db, animal_id) for animal_id in sorted(touched)]
