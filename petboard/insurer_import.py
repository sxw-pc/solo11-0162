"""Insurer portal and bank statement evidence.

A portal approval is an authorization, not money. Only a bank transfer with
status ``settled`` moves funds, and a reversal gives the credited amount back.
Both files are appended to ``file_journal`` verbatim by the CLI so the audit
can replay them in arrival order.
"""
from .source import record

TRANSFER_STATUSES = ('settled', 'returned', 'reversed')


def _validate(db, value, kind):
    claim = db.execute('SELECT animal_id FROM medical_claim WHERE claim_id=?', (value['claim_id'],)).fetchone()
    if not claim:
        raise ValueError(f'{kind} must cite existing medical claim')
    if value.get('animal_id') is not None and value['animal_id'] != claim['animal_id']:
        raise ValueError(f'{kind} animal does not match cited claim')
    return claim['animal_id']


def import_data(db, data):
    touched = set()
    with db:
        for value in data.get('approvals', []):
            if value['decision'] not in ('approved', 'denied'):
                raise ValueError('unsupported insurance review decision')
            if value.get('approved_amount', 0) < 0:
                raise ValueError('approved amount cannot be negative')
            touched.add(_validate(db, value, 'insurance approval'))
            if record(db, value['event_id'], 'insurance_approval', value):
                db.execute('INSERT INTO insurance_approval(event_id,claim_id,at,reviewer,decision,approved_amount,note) VALUES(?,?,?,?,?,?,?)',
                           (value['event_id'], value['claim_id'], value['at'], value['reviewer'], value['decision'],
                            value.get('approved_amount', 0), value.get('note')))
        for value in data.get('transfers', []):
            if value['status'] not in TRANSFER_STATUSES:
                raise ValueError('unsupported bank transfer status')
            if value['amount'] <= 0:
                raise ValueError('bank transfer amount must be positive')
            if value['status'] in ('returned', 'reversed') and not value.get('reverses'):
                raise ValueError('returned or reversed transfer must cite its original transfer')
            if value['status'] == 'reversed' and value['reverses'] == value['transaction_id']:
                raise ValueError('a ledger event cannot reverse itself')
            touched.add(_validate(db, value, 'bank transfer'))
            known = db.execute('SELECT claim_id,payer,payee,amount,status FROM bank_transfer WHERE transaction_id=?',
                               (value['transaction_id'],)).fetchone()
            if known:
                if any(known[k] != value[k] for k in ('claim_id', 'payer', 'payee', 'amount')):
                    raise ValueError('one bank transaction cannot fund two different parties')
                if value['status'] == 'settled' and known['status'] == 'settled':
                    raise ValueError('a settled bank transaction cannot be counted twice')
            if record(db, value['event_id'], 'bank_transfer', value):
                db.execute('INSERT INTO bank_transfer(event_id,claim_id,transaction_id,payer,payee,amount,at,status,reverses,memo) VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (value['event_id'], value['claim_id'], value['transaction_id'], value['payer'], value['payee'],
                            value['amount'], value['at'], value['status'], value.get('reverses'), value.get('memo')))
        from .medical_claim import decide_by_animal
        return [result for animal_id in sorted(touched) for result in decide_by_animal(db, animal_id)]
