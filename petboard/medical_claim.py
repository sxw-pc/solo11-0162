"""Medical cost claim, insurance adjudication and fund clearing.

Three independent money facts are kept apart:

* hospital bill lines   -- what treatment actually cost (never rewritten);
* insurer approval      -- what the contract authorizes (not money);
* bank transfers        -- money that actually moved, and where it went.

A claim is payable only when the return condition differs from the intake
certificate, the visit falls inside both the policy period and the boarding
period, the chip on certificate/bill/policy matches, and no duplicate bill
lines exist.  A later independent ``preexisting`` review withdraws a pending
claim but never rewrites a settled bank transfer; the money becomes a
recovery receivable from whoever received it.  Boarding fees frozen by
``handoff.py`` live in ``fee_line`` and never enter this ledger.
"""
import json
from .source import record
from .store import update_claim_decision


def decide(db, claim_id):
    claim = db.execute('SELECT * FROM medical_claim WHERE claim_id=?', (claim_id,)).fetchone()
    if not claim:
        return None
    animal_id = claim['animal_id']
    animal = db.execute('SELECT * FROM animal WHERE id=?', (animal_id,)).fetchone()
    intake = db.execute('SELECT * FROM intake_decision WHERE animal_id=?', (animal_id,)).fetchone()
    handoff = db.execute('SELECT * FROM handoff WHERE event_id=?', (claim['handoff_id'],)).fetchone()
    policy = db.execute('SELECT * FROM medical_policy WHERE claim_id=?', (claim_id,)).fetchone()
    bills = db.execute('SELECT * FROM hospital_bill WHERE claim_id=? ORDER BY line_no,event_id', (claim_id,)).fetchall()
    approval = db.execute('SELECT * FROM insurance_approval WHERE claim_id=? ORDER BY at,event_id DESC LIMIT 1',
                          (claim_id,)).fetchone()
    transfers = [dict(row) for row in db.execute(
        'SELECT * FROM bank_transfer WHERE claim_id=? ORDER BY at,event_id', (claim_id,))]

    bill_total = sum(row['amount'] for row in bills)
    deductible = policy['deductible'] if policy else 0
    new_injury = _is_new_injury(db, animal_id, intake, handoff)
    cleared = _cleared_as_preexisting(db, animal_id, claim['handoff_id'])
    policy_ok = bool(policy and policy['coverage_from'] <= claim['visit_at'][:10] <= policy['coverage_to'])
    identity_ok, identity_reason = _identity_ok(db, claim, policy, bills, intake)
    visit_in_stay = bool(animal and animal['arrival'][:10] <= claim['visit_at'][:10] <= animal['departure'][:10])
    duplicates = _has_duplicate_lines(bills)
    approved = bool(approval and approval['decision'] == 'approved')
    claimable = (bool(bills) and approved and new_injury and not cleared and policy_ok
                 and identity_ok and visit_in_stay and not duplicates)
    covered = min(bill_total, policy['coverage_limit']) if claimable else 0
    contract_share = max(0, covered - deductible)
    insurance_payable = min(contract_share, approval['approved_amount']) if claimable else 0

    # ---- clearing: replay bank transfers in arrival order ----
    _mark_reversals(transfers)
    used = _classify_transfers(claim, policy, transfers)
    insurer_to_hospital = sum(t['amount'] for t in used['insurer_hospital'])
    insurer_to_owner = sum(t['amount'] for t in used['insurer_owner'])
    owner_to_hospital = sum(t['amount'] for t in used['owner_hospital'])
    hospital_received = owner_to_hospital + insurer_to_hospital
    hospital_outstanding = max(0, bill_total - hospital_received)
    owner_out_of_pocket = max(0, owner_to_hospital - insurer_to_owner)
    # Reimbursement still due to the owner is what they advanced beyond their
    # own deductible share and have not yet been paid back by the insurer.
    owner_pending = max(0, owner_to_hospital - deductible - insurer_to_owner) if claimable else 0
    insurance_outstanding = max(0, insurance_payable - insurer_to_hospital - insurer_to_owner) if claimable else 0
    route = _route(used)

    # ---- money that must be clawed back after a preexisting review ----
    # Only insurer money loses its basis; owner payments for their own pet's
    # treatment stay owed.  Settled transfers are never re-labelled as failed.
    recovery = []
    if cleared and policy:
        for item in transfers:
            if item['effective'] and item['payer'] == policy['insurer']:
                recovery.append(dict(party=item['payee'], amount=item['amount'],
                                     reason='preexisting injury reclassified; insurer transfer must be recovered',
                                     basis=item['transaction_id']))

    # ---- state ----
    if not animal or not intake or intake['state'] != 'accepted':
        state, reason = 'blocked', 'accepted intake evidence missing'
    elif not bills:
        state, reason = 'pending', 'hospital bill lines missing'
    elif not policy:
        state, reason = 'pending', 'insurance policy evidence missing'
    elif duplicates:
        state, reason = 'disputed', 'duplicate hospital bill line ids'
    elif not identity_ok:
        state, reason = 'disputed', identity_reason
    elif cleared:
        state, reason = ('recovered', 'preexisting review: claim withdrawn, settled funds become recovery') \
            if recovery else ('withdrawn', 'preexisting review: pending claim withdrawn')
    elif not new_injury:
        state, reason = 'rejected', 'return condition is not a documented new injury'
    elif not policy_ok:
        state, reason = 'rejected', 'visit outside insurance coverage period'
    elif not visit_in_stay:
        state, reason = 'rejected', 'visit outside boarding period'
    elif not approval:
        state, reason = 'under_review', 'insurer authorization missing'
    elif approval['decision'] != 'approved':
        state, reason = 'denied', 'insurer denied the claim'
    elif not claimable:
        state, reason = 'rejected', 'claim conditions are not met'
    elif hospital_outstanding > 0:
        state, reason = 'partial', f'hospital still owed {hospital_outstanding} for billed treatment'
    elif insurance_outstanding > 0:
        state, reason = 'approved_unpaid', f'insurer still owes {insurance_outstanding}'
    else:
        state, reason = 'settled', 'hospital paid in full and insurer obligation discharged'

    _sync_recovery(db, claim_id, recovery)
    return update_claim_decision(db, claim_id, animal_id, dict(
        state=state, reason=reason, bill_total=bill_total, covered_medical=covered,
        deductible=deductible, insurance_payable=insurance_payable,
        insurance_paid=insurer_to_hospital + insurer_to_owner,
        insurer_paid_hospital=insurer_to_hospital, insurer_paid_owner=insurer_to_owner,
        owner_paid_hospital=owner_to_hospital, owner_reimbursed=insurer_to_owner,
        owner_out_of_pocket=owner_out_of_pocket, hospital_received=hospital_received,
        hospital_outstanding=hospital_outstanding, insurance_outstanding=insurance_outstanding,
        owner_pending_reimbursement=owner_pending, recovery_due=sum(r['amount'] for r in recovery),
        settlement_route=route), approval['event_id'] if approval else None)


def _is_new_injury(db, animal_id, intake, handoff):
    if not intake or not intake['certificate_id'] or not handoff:
        return False
    cert = db.execute('SELECT condition FROM health_certificate WHERE event_id=?',
                      (intake['certificate_id'],)).fetchone()
    if not cert or handoff['condition'] == cert['condition']:
        return False
    return bool(db.execute('SELECT 1 FROM injury_case WHERE animal_id=?', (animal_id,)).fetchone())


def _cleared_as_preexisting(db, animal_id, handoff_id):
    animal = db.execute('SELECT owner FROM animal WHERE id=?', (animal_id,)).fetchone()
    row = db.execute('SELECT * FROM injury_resolution WHERE animal_id=? AND handoff_id=? ORDER BY event_id DESC LIMIT 1',
                     (animal_id, handoff_id)).fetchone()
    return bool(row and animal and row['veterinarian'] != row['owner']
                and row['owner'] == animal['owner'] and row['decision'] == 'preexisting')


def _identity_ok(db, claim, policy, bills, intake):
    cert = db.execute('SELECT chip FROM health_certificate WHERE event_id=?',
                      (intake['certificate_id'],)).fetchone() if intake and intake['certificate_id'] else None
    if not cert:
        return False, 'intake health certificate missing'
    if claim['chip'] != claim['animal_id'] or cert['chip'] != claim['chip']:
        return False, 'claim chip does not match animal and certificate'
    if any(row['chip'] != claim['chip'] for row in bills):
        return False, 'hospital bill chip does not match claim'
    if policy and (policy['chip'] != claim['chip'] or policy['animal_id'] != claim['animal_id']
                   or policy['claim_id'] != claim['claim_id']):
        return False, 'policy identity does not match claim'
    if any(row['hospital'] != claim['hospital'] or row['visit_at'] != claim['visit_at'] for row in bills):
        return False, 'bill line hospital or visit time disagrees with claim'
    return True, ''


def _has_duplicate_lines(bills):
    seen = set()
    for row in bills:
        key = (row['hospital'], row['visit_at'], row['line_no'])
        if key in seen:
            return True
        seen.add(key)
    return False


def _mark_reversals(rows):
    """A settled transfer stays effective unless a later return/reversal row
    cites its transaction.  A reversal row itself never adds money."""
    for item in rows:
        item['effective'] = item['status'] == 'settled'
    for item in rows:
        if item['status'] in ('returned', 'reversed') and item['reverses']:
            for original in rows:
                if original['transaction_id'] == item['reverses']:
                    original['effective'] = False
    for item in rows:
        if item['status'] != 'settled':
            item['effective'] = False


def _classify_transfers(claim, policy, rows):
    """Classify by payer and payee, never by amount.  A single transaction id
    therefore cannot settle both the hospital and the owner."""
    insurer = policy['insurer'] if policy else None
    used = dict(insurer_hospital=[], insurer_owner=[], owner_hospital=[])
    for item in rows:
        if not item['effective']:
            continue
        if insurer and item['payer'] == insurer:
            (used['insurer_hospital'] if item['payee'] == claim['hospital'] else used['insurer_owner']).append(item)
        elif item['payee'] == claim['hospital']:
            used['owner_hospital'].append(item)
    return used


def _route(used):
    hospital = bool(used['insurer_hospital'])
    owner = bool(used['insurer_owner'])
    if hospital and owner:
        return 'mixed'
    if hospital:
        return 'direct_to_hospital'
    if owner:
        return 'reimburse_owner'
    if used['owner_hospital']:
        return 'owner_prepaid_pending_insurance'
    return 'none'


def _sync_recovery(db, claim_id, items):
    db.execute('DELETE FROM recovery_item WHERE claim_id=?', (claim_id,))
    for item in items:
        db.execute('INSERT INTO recovery_item(claim_id,party,amount,reason,basis,status) VALUES(?,?,?,?,?,?)',
                   (claim_id, item['party'], item['amount'], item['reason'], item['basis'], 'due'))


def _claim(db, claim_id):
    claim = db.execute('SELECT * FROM medical_claim WHERE claim_id=?', (claim_id,)).fetchone()
    if not claim:
        raise ValueError(f'claim does not exist: {claim_id}')
    return claim


def decide_by_animal(db, animal_id):
    results = [decide(db, row['claim_id']) for row in
               db.execute('SELECT claim_id FROM medical_claim WHERE animal_id=? ORDER BY claim_id', (animal_id,))]
    return results


def import_data(db, data):
    touched = set()
    with db:
        for value in data.get('claims', []):
            animal = db.execute('SELECT * FROM animal WHERE id=?', (value['animal_id'],)).fetchone()
            if not animal or animal['owner'] != value['owner']:
                raise ValueError('claim owner must match boarded animal')
            handoff = db.execute('SELECT animal_id FROM handoff WHERE event_id=?', (value['handoff_id'],)).fetchone()
            if not handoff or handoff['animal_id'] != value['animal_id']:
                raise ValueError('claim must cite existing handoff for same animal')
            if value['chip'] != value['animal_id']:
                raise ValueError('claim chip must match animal identity')
            if not (animal['arrival'][:10] <= value['visit_at'][:10] <= animal['departure'][:10]):
                raise ValueError('visit must fall inside the boarding stay')
            touched.add(value['animal_id'])
            if record(db, value['event_id'], 'medical_claim', value):
                db.execute('INSERT INTO medical_claim(event_id,claim_id,animal_id,chip,hospital,visit_at,handoff_id) VALUES(?,?,?,?,?,?,?)',
                           (value['event_id'], value['claim_id'], value['animal_id'], value['chip'],
                            value['hospital'], value['visit_at'], value['handoff_id']))
        for value in data.get('policies', []):
            claim = _claim(db, value['claim_id'])
            if value['deductible'] < 0 or value['coverage_limit'] < 0:
                raise ValueError('deductible and coverage limit cannot be negative')
            if value['coverage_from'] > value['coverage_to']:
                raise ValueError('coverage period is inverted')
            if value['chip'] != claim['chip'] or value['animal_id'] != claim['animal_id']:
                raise ValueError('policy identity does not match claim')
            touched.add(claim['animal_id'])
            if record(db, value['event_id'], 'medical_policy', value):
                db.execute('INSERT INTO medical_policy(event_id,claim_id,animal_id,chip,insurer,policy_no,coverage_from,coverage_to,deductible,coverage_limit) VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (value['event_id'], value['claim_id'], claim['animal_id'], value['chip'], value['insurer'],
                            value['policy_no'], value['coverage_from'], value['coverage_to'],
                            value['deductible'], value['coverage_limit']))
        for value in data.get('bills', []):
            claim = _claim(db, value['claim_id'])
            if value['amount'] <= 0:
                raise ValueError('hospital bill line amount must be positive')
            touched.add(claim['animal_id'])
            if record(db, value['event_id'], 'hospital_bill', value):
                db.execute('INSERT INTO hospital_bill(event_id,claim_id,chip,hospital,visit_at,line_no,description,amount) VALUES(?,?,?,?,?,?,?,?)',
                           (value['event_id'], value['claim_id'], value['chip'], value['hospital'], value['visit_at'],
                            str(value['line_no']), value['description'], value['amount']))
        return [result for animal_id in sorted(touched) for result in decide_by_animal(db, animal_id)]
