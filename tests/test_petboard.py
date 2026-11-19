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


if __name__ == '__main__':
    unittest.main()
