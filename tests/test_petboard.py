import copy
import itertools
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


ARB_STEPS = [
    ('care-import', 'arbitration/00_care_plan_and_three_level_chain.json'),
    ('care-import', 'arbitration/01_care_corrections_before_parents.json'),
    ('care-import', 'arbitration/02_care_revoke_third_level.json'),
    ('care-import', 'arbitration/03_care_late_parents_arrive.json'),
    ('handoff-import', 'arbitration/04_handoff_ok.json'),
    ('arbitration-import', 'arbitration/05_arbitration_owner_ack_m2.json'),
    ('arbitration-import', 'arbitration/06_arbitration_owner_revocation.json'),
    ('arbitration-import', 'arbitration/07_arbitration_panel_m3.json'),
]


class ArbitrationLineageCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')
        feed(self.db, 'intake-import', fixture('intake_ok.json'))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def state(self, table):
        return dict(self.db.execute(f'SELECT * FROM {table} WHERE animal_id=?', ('chip-101',)).fetchone())

    def chain(self, root_id):
        rows = self.db.execute('SELECT * FROM care_chain_ledger WHERE animal_id=? AND root_id=? ORDER BY id DESC',
                               ('chip-101', root_id)).fetchall()
        return dict(rows[0])

    def feed_steps(self, steps):
        for command, name in steps:
            feed(self.db, command, fixture(name))

    def full_scenario(self):
        self.feed_steps(ARB_STEPS)

    def test_correction_before_parent_is_pending_and_not_counted(self):
        self.feed_steps(ARB_STEPS[:2])
        care = self.state('care_decision')
        self.assertEqual(care['state'], 'frozen')
        self.assertEqual(self.chain('m2-owner-tip')['status'], 'pending_parent')
        self.assertEqual(care['compensation'], 0)
        self.assertEqual(self.state('handoff_decision')['payable_fee'], 0)

    def test_three_level_chain_revocation_reactivates_middle_tip(self):
        self.feed_steps(ARB_STEPS[:4])
        ledger = self.chain('m4-root-meal')
        self.assertEqual(ledger['status'], 'single_active')
        self.assertEqual(ledger['adopted_tip_id'], 'm5-second-level')
        nodes = {n['event_id']: n for n in json.loads(ledger['nodes'])}
        self.assertIn('withdrawn', nodes['m6-third-level']['excluded_reason'])

    def test_fork_without_arbitration_freezes_fee_never_double_counts(self):
        self.feed_steps(ARB_STEPS[:5])
        ledger = self.chain('m1-original')
        self.assertEqual(ledger['status'], 'fork_pending')
        active = {n['event_id'] for n in json.loads(ledger['nodes']) if n['active']}
        self.assertEqual(active, {'m2-owner-tip', 'm3-panel-tip'})
        self.assertEqual(self.state('care_decision')['compensation'], 0)
        handoff = self.state('handoff_decision')
        self.assertEqual((handoff['state'], handoff['payable_fee'], handoff['frozen_fee']),
                         ('disputed', 0, 4500))

    def test_owner_ack_route_fulfills_meal_without_credit(self):
        self.feed_steps(ARB_STEPS[:6])
        self.assertEqual(self.chain('m1-original')['status'], 'adopted_tip')
        self.assertEqual(self.chain('m1-original')['adopted_tip_id'], 'm2-owner-tip')
        self.assertEqual(self.state('care_decision')['state'], 'fulfilled')
        handoff = self.state('handoff_decision')
        self.assertEqual((handoff['state'], handoff['payable_fee']), ('settled', 4500))
        self.assertEqual(self.db.execute("SELECT amount FROM fee_line WHERE kind='care_credit'").fetchone()[0], 0)

    def test_owner_revocation_refreezes_settled_handoff(self):
        self.feed_steps(ARB_STEPS[:7])
        self.assertEqual(self.chain('m1-original')['status'], 'fork_pending')
        handoff = self.state('handoff_decision')
        self.assertEqual((handoff['state'], handoff['payable_fee'], handoff['frozen_fee']),
                         ('disputed', 0, 4500))
        history = [dict(r) for r in self.db.execute(
            "SELECT handoff_version,kind,amount,frozen FROM fee_line_history WHERE kind='boarding' ORDER BY id")]
        self.assertEqual([(r['amount'], r['frozen']) for r in history], [(4500, 1), (4500, 0)])

    def test_panel_ruling_route_credits_missed_meal(self):
        self.full_scenario()
        ledger = self.chain('m1-original')
        self.assertEqual((ledger['status'], ledger['adopted_tip_id'], ledger['arbitration_id']),
                         ('adopted_tip', 'm3-panel-tip', 'arb-panel-m3'))
        care = self.state('care_decision')
        self.assertEqual((care['state'], care['missed_meals'], care['compensation']), ('partial', 1, 200))
        handoff = self.state('handoff_decision')
        self.assertEqual((handoff['state'], handoff['payable_fee']), ('settled', 4300))
        self.assertEqual(self.db.execute("SELECT amount FROM fee_line WHERE kind='care_credit'").fetchone()[0], -200)

    def test_ledger_keeps_every_decision_version_with_exclusion_reasons(self):
        self.full_scenario()
        rows = [dict(r) for r in self.db.execute(
            'SELECT decision_version,status,adopted_tip_id,arbitration_id FROM care_chain_ledger WHERE root_id=? ORDER BY id',
            ('m1-original',))]
        statuses = [(r['status'], r['adopted_tip_id']) for r in rows]
        self.assertIn(('fork_pending', None), statuses)
        self.assertIn(('adopted_tip', 'm2-owner-tip'), statuses)
        self.assertEqual(statuses[-1], ('adopted_tip', 'm3-panel-tip'))
        # The fork_pending re-entry after owner revocation is preserved (no history overwrite).
        self.assertGreaterEqual(statuses.count(('fork_pending', None)), 2)
        latest = json.loads(self.chain('m1-original')['nodes'])
        excluded = {n['event_id']: n['excluded_reason'] for n in latest if not n['active']}
        self.assertTrue(all(reason for reason in excluded.values()))

    def test_evidence_route_does_not_follow_event_id_order_or_import_order(self):
        # m2 sorts before m3 lexicographically; the panel must still be able to adopt m3.
        self.feed_steps(ARB_STEPS[:5] + [ARB_STEPS[7]])
        self.assertEqual(self.chain('m1-original')['adopted_tip_id'], 'm3-panel-tip')
        self.assertEqual(self.state('handoff_decision')['payable_fee'], 4300)

    def test_different_import_orders_reach_identical_adjudication(self):
        # Revocations must follow the evidence they revoke; everything else may
        # arrive in any order (including corrections long before their parents).
        # End state: a single panel ruling, so the outcome must be identical and
        # must never be decided by event_id ordering (m2 sorts before m3).
        steps = [ARB_STEPS[0], ARB_STEPS[1], ARB_STEPS[2], ARB_STEPS[3],
                 ARB_STEPS[4], ARB_STEPS[7]]
        # Only structural predecessor: handoff(4) needs plans(0) and the panel(5)
        # only adjudicates once actions (3) exist. Revocations and corrections may
        # both arrive in any position, including after their parents.
        constraints = {4: [0], 5: [3]}  # index -> required predecessors
        outcomes = []
        permutations_seen = 0
        for perm in itertools.permutations(range(len(steps))):
            position = {step: i for i, step in enumerate(perm)}
            if not all(all(position[req] < position[step] for req in reqs)
                       for step, reqs in constraints.items()):
                continue
            permutations_seen += 1
            db = connect(Path(self.tmp.name) / f'perm-{len(outcomes)}.sqlite')
            feed(db, 'intake-import', fixture('intake_ok.json'))
            for index in perm:
                command, name = steps[index]
                feed(db, command, fixture(name))
            item = report(db)['animals'][0]
            # Versioned lineage history and version counters legitimately differ by
            # arrival order; the terminal adjudication and fees must be identical.
            care = {k: v for k, v in item['care'].items() if k != 'version'}
            handoff = {k: v for k, v in item['handoff'].items() if k != 'version'}
            terminal = (care, handoff, item['fee_lines'],
                        sorted((c['root_id'], c['status'], c['adopted_tip_id'], c['arbitration_id'])
                               for c in item['care_chains']))
            outcomes.append(terminal)
            db.close()
        self.assertGreaterEqual(permutations_seen, 10)
        self.assertEqual(len({json.dumps(o, sort_keys=True) for o in outcomes}), 1)
        self.assertEqual(outcomes[0][0]['state'], 'partial')
        self.assertEqual(outcomes[0][1]['payable_fee'], 4300)

    def test_cycle_is_detected_and_freezes_chain(self):
        feed(self.db, 'care-import', {'plans': [
            {'event_id': 'plan-9', 'animal_id': 'chip-101', 'version': 9, 'food': 'chicken',
             'medicine': '', 'meals': 1, 'approved_by': 'owner'}],
            'actions': [
                {'event_id': 'cyc-a', 'animal_id': 'chip-101', 'plan_id': 'plan-9', 'kind': 'meal',
                 'day': '2026-11-02', 'actor': 'nurse-a', 'witness': 'checker-b', 'food': 'chicken'},
                {'event_id': 'cyc-b', 'animal_id': 'chip-101', 'plan_id': 'plan-9', 'kind': 'meal',
                 'day': '2026-11-02', 'actor': 'nurse-a', 'witness': 'checker-b', 'food': 'chicken',
                 'supersedes': 'cyc-a'},
                {'event_id': 'cyc-c', 'animal_id': 'chip-101', 'plan_id': 'plan-9', 'kind': 'meal',
                 'day': '2026-11-02', 'actor': 'nurse-a', 'witness': 'checker-b', 'food': 'chicken',
                 'supersedes': 'cyc-b'}]})
        # Close the loop directly in storage (an event_id conflict would block JSON replay).
        with self.db:
            self.db.execute('UPDATE care_action SET supersedes=? WHERE event_id=?', ('cyc-c', 'cyc-a'))
        from petboard.care import decide
        decide(self.db, 'chip-101')
        # A cycle has no root, so the ledger row is looked up by plan and status.
        ledger = dict(self.db.execute(
            "SELECT * FROM care_chain_ledger WHERE plan_id='plan-9' ORDER BY id DESC LIMIT 1").fetchone())
        self.assertEqual(ledger['status'], 'cyclic')
        self.assertEqual(self.state('care_decision')['state'], 'disputed')

    def test_cross_plan_correction_arriving_with_parent_is_rejected_at_import(self):
        feed(self.db, 'care-import', {'plans': [
            {'event_id': 'plan-8', 'animal_id': 'chip-101', 'version': 8, 'food': 'chicken',
             'medicine': '', 'meals': 1, 'approved_by': 'owner'}],
            'actions': [{'event_id': 'cross-1', 'animal_id': 'chip-101', 'plan_id': 'plan-8',
                         'kind': 'meal', 'day': '2026-11-02', 'actor': 'nurse-a',
                         'witness': 'checker-b', 'food': 'chicken'}]})
        with self.assertRaisesRegex(ValueError, 'same animal, plan and kind'):
            feed(self.db, 'care-import', {'actions': [
                {'event_id': 'cross-2', 'animal_id': 'chip-101', 'plan_id': 'plan-1', 'kind': 'meal',
                 'day': '2026-11-02', 'actor': 'nurse-a', 'witness': 'checker-b', 'food': 'chicken',
                 'supersedes': 'cross-1'}]})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM evidence WHERE event_id=?', ('cross-2',)).fetchone()[0], 0)

    def test_late_arriving_cross_plan_parent_freezes_chain(self):
        feed(self.db, 'care-import', {'plans': [
            {'event_id': 'plan-7', 'animal_id': 'chip-101', 'version': 7, 'food': 'chicken',
             'medicine': '', 'meals': 1, 'approved_by': 'owner'}],
            'actions': [{'event_id': 'late-x', 'animal_id': 'chip-101', 'plan_id': 'plan-7',
                         'kind': 'meal', 'day': '2026-11-02', 'actor': 'nurse-a',
                         'witness': 'checker-b', 'food': 'chicken', 'supersedes': 'late-root'}]})
        self.assertEqual(self.chain('late-x')['status'], 'pending_parent')
        feed(self.db, 'care-import', {'plans': [
            {'event_id': 'plan-6', 'animal_id': 'chip-101', 'version': 6, 'food': 'chicken',
             'medicine': '', 'meals': 1, 'approved_by': 'owner'}],
            'actions': [{'event_id': 'late-root', 'animal_id': 'chip-101', 'plan_id': 'plan-6',
                         'kind': 'meal', 'day': '2026-11-02', 'actor': 'nurse-a',
                         'witness': 'checker-b', 'food': 'chicken'}]})
        self.assertEqual(self.chain('late-x')['status'], 'cross_plan')

    def test_conflicting_arbitrations_keep_fee_frozen(self):
        self.feed_steps(ARB_STEPS[:5])
        feed(self.db, 'arbitration-import', {'arbitrations': [
            {'event_id': 'arb-owner-m2', 'animal_id': 'chip-101', 'root_id': 'm1-original',
             'decision': 'owner_ack', 'tip_id': 'm2-owner-tip', 'signed_by': 'owner-a',
             'at': '2026-11-04T18:00', 'rationale': 'owner picks m2'}]})
        feed(self.db, 'arbitration-import', {'arbitrations': [
            {'event_id': 'arb-panel-m3', 'animal_id': 'chip-101', 'root_id': 'm1-original',
             'decision': 'panel_ruling', 'tip_id': 'm3-panel-tip', 'signed_by': 'nurse-e',
             'witness': 'nurse-f', 'at': '2026-11-05T11:00', 'rationale': 'panel picks m3'}]})
        self.assertEqual(self.chain('m1-original')['status'], 'conflicted_arbitration')
        self.assertEqual(self.state('handoff_decision')['payable_fee'], 0)

    def test_panel_without_independent_witness_is_invalid_arbitration(self):
        self.feed_steps(ARB_STEPS[:5])
        feed(self.db, 'arbitration-import', {'arbitrations': [
            {'event_id': 'arb-bad-panel', 'animal_id': 'chip-101', 'root_id': 'm1-original',
             'decision': 'panel_ruling', 'tip_id': 'm3-panel-tip', 'signed_by': 'nurse-c',
             'witness': 'checker-d', 'at': '2026-11-05T11:00', 'rationale': 'witness is the tip witness'}]})
        self.assertEqual(self.chain('m1-original')['status'], 'invalid_arbitration')

    def test_owner_invalidation_marks_meal_not_served(self):
        feed(self.db, 'care-import', {'plans': [
            {'event_id': 'plan-5', 'animal_id': 'chip-101', 'version': 5, 'food': 'chicken',
             'medicine': '', 'meals': 1, 'approved_by': 'owner'}],
            'actions': [{'event_id': 'inv-root', 'animal_id': 'chip-101', 'plan_id': 'plan-5',
                         'kind': 'meal', 'day': '2026-11-02', 'actor': 'nurse-a',
                         'witness': 'checker-b', 'food': 'chicken'}]})
        feed(self.db, 'arbitration-import', {'arbitrations': [
            {'event_id': 'arb-invalid-1', 'animal_id': 'chip-101', 'root_id': 'inv-root',
             'decision': 'invalid', 'signed_by': 'owner-a', 'at': '2026-11-03T10:00',
             'rationale': 'owner proves the meal never happened'}]})
        self.assertEqual(self.chain('inv-root')['status'], 'invalidated_root')
        self.assertEqual(self.state('care_decision')['compensation'], 200)

    def test_arbitration_revocation_requires_original_signer(self):
        self.feed_steps(ARB_STEPS[:6])
        with self.assertRaisesRegex(ValueError, 'original signer'):
            feed(self.db, 'arbitration-import', {'arbitration_revocations': [
                {'event_id': 'arb-revoke-bad', 'arbitration_id': 'arb-owner-m2', 'animal_id': 'chip-101',
                 'at': '2026-11-05T09:30', 'signed_by': 'nurse-e', 'reason': 'forged revocation'}]})

    def test_arbitration_batch_conflict_rolls_back_atomically(self):
        self.feed_steps(ARB_STEPS[:5])
        before = self.db.execute('SELECT COUNT(*) FROM arbitration_evidence').fetchone()[0]
        payload = {'arbitrations': [
            {'event_id': 'arb-ok', 'animal_id': 'chip-101', 'root_id': 'm1-original',
             'decision': 'owner_ack', 'tip_id': 'm2-owner-tip', 'signed_by': 'owner-a',
             'at': '2026-11-04T18:00', 'rationale': 'ok'},
            {'event_id': 'arb-ok', 'animal_id': 'chip-101', 'root_id': 'm1-original',
             'decision': 'panel_ruling', 'tip_id': 'm3-panel-tip', 'signed_by': 'nurse-e',
             'witness': 'nurse-f', 'at': '2026-11-05T11:00', 'rationale': 'same id different content'}]}
        with self.assertRaisesRegex(ValueError, 'event_id conflict'):
            feed(self.db, 'arbitration-import', payload)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM arbitration_evidence').fetchone()[0], before)
        self.assertEqual(self.chain('m1-original')['status'], 'fork_pending')

    def test_full_scenario_audit_replay_matches(self):
        self.full_scenario()
        self.assertEqual(audit(self.db)['status'], 'matched')


if __name__ == '__main__':
    unittest.main()
