import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import petboard
from petboard.__main__ import run_import, audit
from petboard.report import report
from petboard.store import connect

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'


def fixture(name):
    return json.loads((FIX / name).read_text(encoding='utf8'))


def feed(db, kind, value):
    return run_import(db, kind, json.dumps(value, ensure_ascii=False).encode())


class BoardingCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')
        self.assertEqual(Path(petboard.__file__).resolve().parent, ROOT / 'petboard')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def normal(self):
        feed(self.db, 'intake-import', fixture('intake_ok.json'))
        feed(self.db, 'care-import', fixture('care_ok.json'))
        feed(self.db, 'handoff-import', fixture('handoff_ok.json'))
        return report(self.db)['animals'][0]

    def state(self, table):
        return dict(self.db.execute(f'SELECT * FROM {table} WHERE animal_id=?', ('chip-101',)).fetchone())

    def test_normal_cross_subsystem_settlement(self):
        item = self.normal()
        self.assertEqual((item['intake']['state'], item['care']['state'], item['handoff']['state']),
                         ('accepted', 'fulfilled', 'settled'))
        self.assertEqual(item['handoff']['payable_fee'], 4500)
        self.assertGreaterEqual(len(item['history']), 5)

    def test_vaccine_late_change_retracts_care_and_handoff(self):
        self.normal()
        feed(self.db, 'intake-import', fixture('intake_bad.json'))
        self.assertEqual(self.state('intake_decision')['state'], 'rejected')
        self.assertEqual(self.state('care_decision')['state'], 'blocked')
        self.assertNotEqual(self.state('handoff_decision')['state'], 'settled')

    def test_veterinarian_revocation_retracts_settlement(self):
        self.normal()
        feed(self.db, 'intake-import', {'revocations': [{'event_id': 'revoke-1', 'certificate_id': 'cert-1',
            'animal_id': 'chip-101', 'at': '2026-11-03', 'signed_by': 'veterinarian', 'reason': 'invalid lot'}]})
        self.assertEqual(self.state('intake_decision')['state'], 'revoked')
        self.assertEqual(self.state('care_decision')['state'], 'blocked')
        self.assertNotEqual(self.state('handoff_decision')['state'], 'settled')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_requested_medicine_contraindication(self):
        value = fixture('intake_ok.json')
        value['animals'][0]['requested_medicine'] = 'drug-x'
        feed(self.db, 'intake-import', value)
        self.assertIn('medicine', self.state('intake_decision')['reason'])

    def test_parallel_certificate_conflict_preserves_history(self):
        feed(self.db, 'intake-import', fixture('intake_ok.json'))
        a = fixture('intake_bad.json')['certificates'][0]
        a['vaccine_until'] = '2026-12-31'
        feed(self.db, 'intake-import', {'certificates': [a]})
        b = copy.deepcopy(a)
        b['event_id'] = 'cert-3'
        b['condition'] = 'cough'
        feed(self.db, 'intake-import', {'certificates': [b]})
        self.assertEqual(self.state('intake_decision')['state'], 'frozen')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM health_certificate').fetchone()[0], 3)

    def test_contraindicated_plan_changes_care(self):
        self.normal()
        feed(self.db, 'care-import', fixture('care_bad.json'))
        self.assertEqual(self.state('care_decision')['state'], 'blocked')
        self.assertNotEqual(self.state('handoff_decision')['state'], 'settled')

    def test_single_actor_medicine_is_disputed(self):
        feed(self.db, 'intake-import', fixture('intake_ok.json'))
        data = fixture('care_ok.json')
        data['actions'][1]['witness'] = 'nurse-a'
        feed(self.db, 'care-import', data)
        self.assertEqual(self.state('care_decision')['state'], 'disputed')

    def test_missing_prescribed_medicine_is_partial(self):
        feed(self.db, 'intake-import', fixture('intake_ok.json'))
        data = fixture('care_ok.json')
        data['actions'] = [data['actions'][0]]
        feed(self.db, 'care-import', data)
        self.assertEqual(self.state('care_decision')['state'], 'partial')

    def test_late_action_correction_reopens_care_and_fee(self):
        self.normal()
        correction = {'actions': [{'event_id': 'meal-corrected', 'animal_id': 'chip-101',
                                   'plan_id': 'plan-1', 'kind': 'meal', 'day': '2026-11-02',
                                   'actor': 'nurse-a', 'witness': None, 'food': 'chicken',
                                   'supersedes': 'meal-1'}]}
        feed(self.db, 'care-import', correction)
        self.assertEqual(self.state('care_decision')['compensation'], 200)
        self.assertEqual(self.state('handoff_decision')['payable_fee'], 4300)
        self.assertEqual(self.db.execute("SELECT amount FROM fee_line WHERE kind='care_credit'").fetchone()[0], -200)
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_missed_meal_creates_compensation(self):
        feed(self.db, 'intake-import', fixture('intake_ok.json'))
        data = fixture('care_ok.json')
        data['actions'] = [data['actions'][1]]
        feed(self.db, 'care-import', data)
        self.assertEqual(self.state('care_decision')['state'], 'partial')
        self.assertEqual(self.state('care_decision')['compensation'], 200)

    def test_unauthorized_handoff_freezes_fee(self):
        self.normal()
        feed(self.db, 'handoff-import', fixture('handoff_bad.json'))
        result = self.state('handoff_decision')
        self.assertEqual(result['state'], 'blocked')
        self.assertEqual(result['payable_fee'], 0)
        self.assertGreater(result['frozen_fee'], 0)

    def test_new_illness_requires_independent_evidence(self):
        self.normal()
        value = {'handoffs': [{'event_id': 'handoff-3', 'animal_id': 'chip-101', 'at': '2026-11-05',
                               'person': 'picker-a', 'witness': 'staff-b', 'condition': 'cough'}]}
        feed(self.db, 'handoff-import', value)
        self.assertEqual(self.state('handoff_decision')['state'], 'disputed')
        self.assertEqual(self.db.execute('SELECT state FROM injury_case').fetchone()[0], 'unverified')

    def test_documented_new_illness_still_requires_responsibility_review(self):
        self.normal()
        feed(self.db, 'handoff-import', {'handoffs': [{'event_id': 'handoff-6', 'animal_id': 'chip-101',
            'at': '2026-11-05', 'person': 'picker-a', 'witness': 'staff-b', 'condition': 'cough',
            'veterinarian': 'vet-review-a'}]})
        self.assertEqual(self.state('handoff_decision')['state'], 'disputed')
        self.assertEqual(self.db.execute('SELECT state FROM injury_case').fetchone()[0], 'documented')

    def test_independent_injury_resolution_releases_frozen_fee(self):
        self.normal()
        feed(self.db, 'handoff-import', {'handoffs': [{'event_id': 'handoff-6', 'animal_id': 'chip-101',
            'at': '2026-11-05', 'person': 'picker-a', 'witness': 'staff-b', 'condition': 'cough',
            'veterinarian': 'vet-review-a'}]})
        self.assertEqual(self.state('handoff_decision')['state'], 'disputed')
        feed(self.db, 'handoff-import', {'resolutions': [{'event_id': 'resolution-1', 'animal_id': 'chip-101',
            'handoff_id': 'handoff-6', 'veterinarian': 'vet-review-b', 'owner': 'owner-a',
            'decision': 'preexisting', 'rationale': 'intake evidence review'}]})
        self.assertEqual(self.state('handoff_decision')['state'], 'settled')
        self.assertEqual(self.db.execute('SELECT state FROM injury_case').fetchone()[0], 'cleared')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_self_approved_injury_resolution_keeps_dispute(self):
        self.normal()
        feed(self.db, 'handoff-import', {'handoffs': [{'event_id': 'handoff-6', 'animal_id': 'chip-101',
            'at': '2026-11-05', 'person': 'picker-a', 'witness': 'staff-b', 'condition': 'cough'}]})
        feed(self.db, 'handoff-import', {'resolutions': [{'event_id': 'resolution-2', 'animal_id': 'chip-101',
            'handoff_id': 'handoff-6', 'veterinarian': 'owner-a', 'owner': 'owner-a',
            'decision': 'preexisting', 'rationale': 'self approval attempt'}]})
        self.assertEqual(self.state('handoff_decision')['state'], 'disputed')

    def test_overdue_fee_and_disputed_fee_are_separate(self):
        self.normal()
        value = {'handoffs': [{'event_id': 'handoff-4', 'animal_id': 'chip-101', 'at': '2026-11-06',
                               'person': 'picker-a', 'witness': 'staff-b', 'condition': 'healthy'}]}
        feed(self.db, 'handoff-import', value)
        self.assertEqual(self.state('handoff_decision')['overdue_fee'], 3000)
        value['handoffs'][0]['event_id'] = 'handoff-5'
        value['handoffs'][0]['at'] = '2026-11-07'
        value['handoffs'][0]['dispute'] = 'care questioned'
        feed(self.db, 'handoff-import', value)
        self.assertEqual(self.state('handoff_decision')['state'], 'disputed')
        self.assertEqual(self.state('handoff_decision')['payable_fee'], 0)
        self.assertTrue(all(r[0] for r in self.db.execute('SELECT frozen FROM fee_line')))

    def test_idempotent_replay_and_conflicting_id(self):
        value = fixture('intake_ok.json')
        feed(self.db, 'intake-import', value)
        old = self.state('intake_decision')['version']
        feed(self.db, 'intake-import', value)
        self.assertEqual(self.state('intake_decision')['version'], old)
        value['certificates'][0]['condition'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'event_id conflict'):
            feed(self.db, 'intake-import', value)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0], 2)

    def test_atomic_batch_failure(self):
        feed(self.db, 'intake-import', fixture('intake_ok.json'))
        value = fixture('care_ok.json')
        value['actions'][1]['plan_id'] = None
        with self.assertRaises(Exception):
            feed(self.db, 'care-import', value)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM care_plan').fetchone()[0], 0)

    def test_public_cli_demo_and_report(self):
        dbpath = Path(self.tmp.name) / 'cli.sqlite'
        command = [sys.executable, '-m', 'petboard', 'demo', '--db', str(dbpath)]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)['animals'][0]['handoff']['state'], 'settled')
        report_cmd = [sys.executable, '-m', 'petboard', 'report', '--db', str(dbpath)]
        self.assertEqual(json.loads(subprocess.run(report_cmd, cwd=ROOT, capture_output=True, text=True, check=True).stdout)['evidence_count'], 7)

    def test_source_replay_audit_detects_unlogged_mutation(self):
        self.normal()
        self.assertEqual(audit(self.db)['status'], 'matched')
        with self.db:
            self.db.execute("UPDATE handoff_decision SET payable_fee=9 WHERE animal_id='chip-101'")
        with self.assertRaisesRegex(ValueError, 'disagree'):
            audit(self.db)


class MedicalClaimCase(unittest.TestCase):
    KEYS = ('state', 'reason', 'bill_total', 'covered_medical', 'deductible', 'insurance_payable',
            'insurance_paid', 'insurer_paid_hospital', 'insurer_paid_owner', 'owner_paid_hospital',
            'owner_reimbursed', 'owner_out_of_pocket', 'hospital_received', 'hospital_outstanding',
            'insurance_outstanding', 'owner_pending_reimbursement', 'recovery_due', 'settlement_route',
            'version')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')
        feed(self.db, 'intake-import', fixture('intake_ok.json'))
        feed(self.db, 'care-import', fixture('care_ok.json'))
        feed(self.db, 'handoff-import', fixture('handoff_injury.json'))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def claim(self, name='claim_direct.json', claim_id='claim-direct'):
        feed(self.db, 'claim-import', fixture(name))
        return self.decision(claim_id)

    def decision(self, claim_id=None):
        row = self.db.execute('SELECT * FROM medical_claim_decision WHERE claim_id=?',
                              (claim_id or 'claim-direct',)).fetchone()
        return {key: row[key] for key in self.KEYS} if row else None

    def feeds(self, *names, claim_id='claim-direct'):
        for name in names:
            feed(self.db, 'insurer-import', fixture(name))
        return self.decision(claim_id)

    def test_direct_pay_route_settles_hospital_and_spares_owner(self):
        self.claim()
        result = self.feeds('insurer_direct.json')
        self.assertEqual(result['state'], 'settled')
        self.assertEqual((result['bill_total'], result['covered_medical'], result['deductible']), (1200, 1200, 200))
        self.assertEqual(result['insurance_payable'], 1000)
        self.assertEqual(result['insurer_paid_hospital'], 1000)
        self.assertEqual(result['hospital_received'], 1200)
        self.assertEqual(result['hospital_outstanding'], 0)
        self.assertEqual(result['owner_paid_hospital'], 200)
        self.assertEqual(result['owner_out_of_pocket'], 200)
        self.assertEqual(result['owner_pending_reimbursement'], 0)
        self.assertEqual(result['settlement_route'], 'direct_to_hospital')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM recovery_item').fetchone()[0], 0)

    def test_direct_route_without_owner_share_is_not_settled(self):
        data = fixture('insurer_direct.json')
        data['transfers'] = [t for t in data['transfers'] if t['payer'] == 'insurer-alpha']
        feed(self.db, 'claim-import', fixture('claim_direct.json'))
        feed(self.db, 'insurer-import', data)
        result = self.decision()
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['hospital_outstanding'], 200)
        self.assertEqual(result['insurance_outstanding'], 0)
        self.assertEqual(result['owner_out_of_pocket'], 0)

    def test_reimburse_route_counts_payee_not_amount(self):
        self.claim('claim_reimburse.json')
        self.feeds('insurer_reimburse_approval.json', 'insurer_reimburse_bounced.json', 'insurer_reimburse_settled.json')
        result = self.decision('claim-reimburse')
        self.assertEqual(result['state'], 'settled')
        self.assertEqual(result['insurer_paid_hospital'], 0)
        self.assertEqual(result['insurer_paid_owner'], 1000)
        self.assertEqual(result['owner_paid_hospital'], 1200)
        self.assertEqual(result['owner_reimbursed'], 1000)
        self.assertEqual(result['owner_out_of_pocket'], 200)
        self.assertEqual(result['hospital_received'], 1200)
        self.assertEqual(result['settlement_route'], 'reimburse_owner')

    def test_bounced_owner_reimbursement_stays_owed(self):
        self.claim('claim_reimburse.json')
        self.feeds('insurer_reimburse_approval.json', 'insurer_reimburse_bounced.json')
        result = self.decision('claim-reimburse')
        self.assertEqual(result['state'], 'approved_unpaid')
        self.assertEqual(result['insurance_paid'], 0)
        self.assertEqual(result['insurance_outstanding'], 1000)
        self.assertEqual(result['owner_pending_reimbursement'], 1000)
        self.assertEqual(result['owner_out_of_pocket'], 1200)
        self.assertEqual(result['hospital_received'], 1200)

    def test_partial_hospital_collection_before_owner_reimbursement(self):
        self.claim('claim_reimburse.json')
        result = self.feeds('insurer_reimburse_approval.json', 'owner_partial_payment.json', claim_id='claim-reimburse')
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['hospital_received'], 500)
        self.assertEqual(result['hospital_outstanding'], 700)
        self.assertEqual(result['owner_pending_reimbursement'], 300)
        result = self.feeds('owner_final_payment.json', claim_id='claim-reimburse')
        self.assertEqual(result['state'], 'approved_unpaid')
        self.assertEqual(result['hospital_outstanding'], 0)
        self.assertEqual(result['owner_pending_reimbursement'], 1000)

    def test_early_insurer_receipt_does_not_hide_hospital_debt(self):
        self.claim('claim_reimburse.json')
        result = self.feeds('insurer_reimburse_approval.json', 'insurer_early_receipt.json', claim_id='claim-reimburse')
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['insurer_paid_owner'], 1000)
        self.assertEqual(result['hospital_received'], 0)
        self.assertEqual(result['hospital_outstanding'], 1200)
        self.assertEqual(result['owner_out_of_pocket'], 0)

    def test_same_bank_line_cannot_pay_hospital_and_owner(self):
        self.claim()
        self.feeds('insurer_direct.json')
        with self.assertRaisesRegex(ValueError, 'two different parties'):
            self.feeds('insurer_same_txn_reuse.json')

    def test_late_preexisting_review_withdraws_claim_and_recovers_funds(self):
        self.claim('claim_reimburse.json')
        self.feeds('insurer_reimburse_approval.json', 'insurer_reimburse_bounced.json', 'insurer_reimburse_settled.json')
        self.assertEqual(self.decision('claim-reimburse')['state'], 'settled')
        feed(self.db, 'handoff-import', fixture('late_preexisting.json'))
        result = self.decision('claim-reimburse')
        self.assertEqual(result['state'], 'recovered')
        self.assertEqual(result['insurance_payable'], 0)
        self.assertEqual(result['insurance_paid'], 1000)
        self.assertEqual(result['recovery_due'], 1000)
        self.assertEqual(result['hospital_received'], 1200)
        self.assertEqual(result['hospital_outstanding'], 0)
        self.assertEqual(result['owner_out_of_pocket'], 200)
        recovery = [dict(row) for row in self.db.execute('SELECT party,amount,basis FROM recovery_item')]
        self.assertEqual(recovery, [{'party': 'owner-a', 'amount': 1000, 'basis': 'txn-reimburse-ins-2'}])

    def test_preexisting_review_before_payment_withdraws_without_recovery(self):
        self.claim('claim_reimburse.json')
        self.feeds('insurer_reimburse_approval.json')
        feed(self.db, 'handoff-import', fixture('late_preexisting.json'))
        result = self.decision('claim-reimburse')
        self.assertEqual(result['state'], 'withdrawn')
        self.assertEqual(result['insurance_payable'], 0)
        self.assertEqual(result['hospital_received'], 0)
        self.assertEqual(result['recovery_due'], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM recovery_item').fetchone()[0], 0)

    def test_boarding_fee_freeze_is_not_medical_bill(self):
        self.claim()
        self.feeds('insurer_direct.json')
        frozen = self.db.execute("SELECT SUM(frozen_fee) FROM handoff_decision").fetchone()[0]
        self.assertGreater(frozen, 0)
        self.assertEqual(self.decision()['bill_total'], 1200)
        self.assertEqual(self.db.execute('SELECT SUM(amount) FROM fee_line').fetchone()[0], 4500)

    def test_approval_receipt_alone_is_not_payment(self):
        self.claim()
        feed(self.db, 'insurer-import', {'approvals': [{'event_id': 'approval-only-1', 'claim_id': 'claim-direct',
             'at': '2026-11-05', 'reviewer': 'adjuster-a', 'decision': 'approved', 'approved_amount': 1000}]})
        result = self.decision()
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['insurance_payable'], 1000)
        self.assertEqual(result['insurance_paid'], 0)
        self.assertEqual(result['hospital_outstanding'], 1200)

    def test_claim_requires_documented_new_injury_and_matching_chip(self):
        db = connect(Path(self.tmp.name) / 'healthy.sqlite')
        try:
            feed(db, 'intake-import', fixture('intake_ok.json'))
            feed(db, 'care-import', fixture('care_ok.json'))
            feed(db, 'handoff-import', fixture('handoff_ok.json'))
            with self.assertRaisesRegex(ValueError, 'existing handoff'):
                feed(db, 'claim-import', fixture('claim_direct.json'))
        finally:
            db.close()

    def test_claim_import_late_handoff_recomputes(self):
        db = connect(Path(self.tmp.name) / 'late.sqlite')
        try:
            feed(db, 'intake-import', fixture('intake_ok.json'))
            feed(db, 'care-import', fixture('care_ok.json'))
            with self.assertRaisesRegex(ValueError, 'existing handoff'):
                feed(db, 'claim-import', fixture('claim_direct.json'))
            feed(db, 'handoff-import', fixture('handoff_injury.json'))
            feed(db, 'claim-import', fixture('claim_direct.json'))
            row = db.execute('SELECT state FROM medical_claim_decision').fetchone()
            self.assertEqual(row['state'], 'under_review')
            self.assertEqual(audit(db)['status'], 'matched')
        finally:
            db.close()

    def test_claim_replay_is_idempotent_and_conflicting_content_rejected(self):
        value = fixture('claim_direct.json')
        feed(self.db, 'claim-import', value)
        version = self.decision()['version']
        feed(self.db, 'claim-import', value)
        self.assertEqual(self.decision()['version'], version)
        value['bills'][0]['amount'] = 301
        with self.assertRaisesRegex(ValueError, 'event_id conflict'):
            feed(self.db, 'claim-import', value)

    def test_audit_rebuilds_cost_attribution_from_source_files(self):
        self.claim('claim_reimburse.json')
        self.feeds('insurer_reimburse_approval.json', 'insurer_reimburse_bounced.json', 'insurer_reimburse_settled.json')
        self.assertEqual(audit(self.db)['status'], 'matched')
        with self.db:
            self.db.execute("UPDATE medical_claim_decision SET insurer_paid_owner=0 WHERE claim_id='claim-reimburse'")
        with self.assertRaisesRegex(ValueError, 'disagree'):
            audit(self.db)

    def test_claim_cli_import_report_audit(self):
        dbpath = Path(self.tmp.name) / 'cli.sqlite'
        steps = [('intake-import', 'intake_ok.json'), ('care-import', 'care_ok.json'),
                 ('handoff-import', 'handoff_injury.json'), ('claim-import', 'claim_direct.json'),
                 ('insurer-import', 'insurer_direct.json')]
        for command, name in steps:
            subprocess.run([sys.executable, '-m', 'petboard', command, '--db', str(dbpath),
                            '--file', str(FIX / name)], cwd=ROOT, capture_output=True, text=True, check=True)
        report_cmd = [sys.executable, '-m', 'petboard', 'report', '--db', str(dbpath)]
        payload = json.loads(subprocess.run(report_cmd, cwd=ROOT, capture_output=True, text=True, check=True).stdout)
        claim = payload['animals'][0]['medical_claims'][0]
        self.assertEqual((claim['decision']['state'], claim['decision']['owner_out_of_pocket']), ('settled', 200))
        self.assertEqual(len(claim['bill_lines']), 2)
        self.assertEqual(len(claim['transfers']), 2)
        audit_cmd = [sys.executable, '-m', 'petboard', 'audit', '--db', str(dbpath)]
        self.assertEqual(json.loads(subprocess.run(audit_cmd, cwd=ROOT, capture_output=True, text=True,
                                                  check=True).stdout)['status'], 'matched')


if __name__ == '__main__':
    unittest.main()