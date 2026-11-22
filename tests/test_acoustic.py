import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from petboard.store import connect
from petboard.__main__ import run_import, audit
from petboard.report import report

REPO = Path(__file__).resolve().parents[1]
FIX = REPO / 'fixtures'
W1 = '2026-11-02T20:00:00/2026-11-02T20:10:00'


def fixture(name):
    return json.loads((FIX / name).read_text(encoding='utf8'))


def feed(db, kind, value):
    return run_import(db, kind, json.dumps(value, ensure_ascii=False).encode())


class AcousticChain(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')
        feed(self.db, 'complaint-import', fixture('complaint_case.json'))
        feed(self.db, 'noise-import', fixture('noise_case.json'))

    def tearDown(self):
        self.db.close()

    def noise(self):
        return dict(self.db.execute('SELECT * FROM noise_decision').fetchone())

    def complaint(self):
        return dict(self.db.execute('SELECT * FROM complaint_decision').fetchone())

    def scope(self):
        return dict(self.db.execute('SELECT * FROM business_scope').fetchone())

    def upheld(self):
        feed(self.db, 'noise-import', fixture('noise_pack.json'))
        feed(self.db, 'noise-import', fixture('experiment_valid.json'))
        self.assertEqual(self.complaint()['state'], 'upheld')

    def test_peak_alone_cannot_uphold(self):
        noise = self.noise()
        self.assertEqual(noise['state'], 'uncertain')
        self.assertEqual(noise['peak_db'], 68.0)  # single-peak rule would have exceeded
        complaint = self.complaint()
        self.assertEqual(complaint['state'], 'pending')
        plan = json.loads(complaint['required_plan'])
        self.assertEqual({entry['kind'] for entry in plan}, {'shutdown', 'barrier'})
        self.assertEqual({entry['window'] for entry in plan}, {W1})
        self.assertEqual({entry['path'] for entry in plan}, {'boundary-west'})

    def test_mixed_clip_blocks_passive_attribution(self):
        feed(self.db, 'noise-import', fixture('noise_pack.json'))
        noise = self.noise()
        self.assertEqual(noise['state'], 'uncertain')
        self.assertIn('clip-m1', noise['uncertainty'])
        self.assertEqual(self.complaint()['state'], 'pending')
        devices = {row['device_id']: row for row in self.db.execute('SELECT * FROM acoustic_device')}
        self.assertEqual(devices['slm-west-1']['location'], 'boundary-west')
        self.assertLessEqual(devices['slm-west-1']['drift_db'], 1.0)
        clip = self.db.execute("SELECT * FROM audio_clip WHERE event_id='clip-m1'").fetchone()
        self.assertEqual(clip['classification'], 'mixed')
        self.assertEqual(clip['window'], W1)
        self.assertTrue(clip['sha'])

    def test_non_comparable_shutdown_is_not_evidence(self):
        feed(self.db, 'noise-import', fixture('noise_pack.json'))
        feed(self.db, 'noise-import', fixture('experiment_invalid.json'))
        noise = self.noise()
        self.assertEqual(noise['state'], 'uncertain')
        self.assertIn('exp-shut-bad', noise['uncertainty'])
        self.assertIn('road background differs', noise['uncertainty'])
        self.assertEqual(self.complaint()['state'], 'pending')

    def test_comparable_shutdown_attributes_energy_dose(self):
        feed(self.db, 'noise-import', fixture('noise_pack.json'))
        feed(self.db, 'noise-import', fixture('experiment_invalid.json'))
        feed(self.db, 'noise-import', fixture('experiment_valid.json'))
        noise = self.noise()
        self.assertEqual(noise['state'], 'exceeded')
        # Energy subtraction, not 68 - 62 = 6 dB arithmetic.
        self.assertAlmostEqual(noise['store_db'], 10 * math.log10(10 ** 6.8 - 10 ** 6.2), places=6)
        self.assertAlmostEqual(noise['store_db'], 66.74, places=2)
        self.assertNotEqual(round(noise['store_db']), 68 - 62)
        self.assertEqual(noise['path'], 'boundary-west')
        self.assertEqual(noise['window'], W1)
        self.assertAlmostEqual(noise['background_db'], 61.8, places=2)
        complaint = self.complaint()
        self.assertEqual(complaint['state'], 'upheld')
        basis = json.loads(complaint['basis'])
        self.assertEqual(basis['windows'], [W1])
        self.assertAlmostEqual(basis['store_db'], 66.74, places=2)
        self.assertIn('shutdown', basis['background_basis'])
        self.assertEqual(basis['invalid_experiments'][0]['event_id'], 'exp-shut-bad')
        self.assertTrue(basis['uncertainty'])
        self.assertIsNone(complaint['required_plan'])

    def test_planned_barrier_is_not_effective(self):
        self.upheld()
        feed(self.db, 'remediation-import', fixture('remediation_barrier_planned.json'))
        scope = self.scope()
        self.assertEqual(scope['state'], 'unverified')
        self.assertEqual(json.loads(scope['commitments']), [])
        self.assertIn('planned', scope['reason'])

    def test_installed_barrier_commits_outdoor_until_21(self):
        self.upheld()
        feed(self.db, 'remediation-import', fixture('remediation_barrier_planned.json'))
        feed(self.db, 'remediation-import', fixture('remediation_barrier_installed.json'))
        scope = self.scope()
        self.assertEqual(scope['state'], 'committed')
        commitments = json.loads(scope['commitments'])
        self.assertEqual(len(commitments), 1)
        self.assertEqual(commitments[0]['route'], 'barrier-west')
        self.assertEqual(commitments[0]['outdoor_until'], '21:00')
        self.assertAlmostEqual(commitments[0]['store_db'], 58.0)
        # Responsibility from the complaint stays on record after remediation.
        self.assertEqual(self.complaint()['state'], 'upheld')

    def test_two_routes_change_committed_hours(self):
        self.upheld()
        feed(self.db, 'remediation-import', fixture('remediation_barrier_installed.json'))
        feed(self.db, 'remediation-import', fixture('remediation_curfew.json'))
        commitments = {c['route']: c for c in json.loads(self.scope()['commitments'])}
        self.assertEqual(commitments['barrier-west']['outdoor_until'], '21:00')
        self.assertAlmostEqual(commitments['barrier-west']['store_db'], 58.0)
        self.assertEqual(commitments['curfew-2000']['outdoor_until'], '20:00')
        self.assertAlmostEqual(commitments['curfew-2000']['store_db'], 54.0)
        self.assertIn('寄养', commitments['curfew-2000']['detail'])

    def test_barrier_check_does_not_substitute_curfew_execution(self):
        self.upheld()
        curfew = fixture('remediation_curfew.json')
        feed(self.db, 'remediation-import', {'remediations': curfew['remediations'], 'checks': []})
        feed(self.db, 'remediation-import', fixture('remediation_barrier_installed.json'))
        routes = {c['route'] for c in json.loads(self.scope()['commitments'])}
        self.assertEqual(routes, {'barrier-west'})
        feed(self.db, 'remediation-import', {'remediations': [], 'checks': curfew['checks']})
        routes = {c['route'] for c in json.loads(self.scope()['commitments'])}
        self.assertEqual(routes, {'barrier-west', 'curfew-2000'})

    def test_passive_separation_dismisses_truck_peak(self):
        db = connect(':memory:')
        try:
            feed(db, 'complaint-import', fixture('complaint_passive.json'))
            feed(db, 'noise-import', fixture('noise_passive.json'))
            noise = dict(db.execute('SELECT * FROM noise_decision').fetchone())
            self.assertEqual(noise['state'], 'clear')
            self.assertEqual(noise['peak_db'], 69.7)  # single-peak rule would have upheld
            self.assertAlmostEqual(noise['store_db'], 64.40, places=2)
            basis = json.loads(noise['basis'])
            self.assertEqual(basis['method'], 'passive')
            self.assertAlmostEqual(basis['background_db'], 52.0)
            self.assertIn('clip-truck-1', basis['clips'])
            complaint = dict(db.execute('SELECT * FROM complaint_decision').fetchone())
            self.assertEqual(complaint['state'], 'dismissed')
            self.assertIsNotNone(complaint['basis'])
        finally:
            db.close()

    def test_report_and_audit_replay(self):
        self.upheld()
        feed(self.db, 'remediation-import', fixture('remediation_barrier_installed.json'))
        feed(self.db, 'remediation-import', fixture('remediation_curfew.json'))
        result = report(self.db)
        case = result['noise_cases'][0]
        self.assertEqual(case['complaint']['state'], 'upheld')
        self.assertEqual(case['scope']['state'], 'committed')
        self.assertEqual(len(case['experiments']), 1)
        self.assertEqual(len(case['checks']), 2)
        states = [(row['subsystem'], row['state']) for row in case['history']]
        self.assertIn(('complaint_decision', 'upheld'), states)
        self.assertIn(('business_scope', 'committed'), states)
        self.assertEqual(len(result['acoustic_devices']), 4)
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_cli_replayable_report_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbpath = str(Path(tmp) / 'chain.sqlite')
            base = [sys.executable, '-B', '-m', 'petboard']
            for command, name in [('complaint-import', 'complaint_case.json'), ('noise-import', 'noise_case.json'),
                                  ('noise-import', 'noise_pack.json'), ('noise-import', 'experiment_valid.json'),
                                  ('remediation-import', 'remediation_barrier_installed.json')]:
                subprocess.run(base + [command, '--db', dbpath, '--file', str(FIX / name)],
                               cwd=REPO, check=True, capture_output=True)
            out = subprocess.run(base + ['report', '--db', dbpath], cwd=REPO, check=True, capture_output=True, text=True).stdout
            case = json.loads(out)['noise_cases'][0]
            self.assertEqual(case['complaint']['state'], 'upheld')
            self.assertEqual(case['scope']['state'], 'committed')
            self.assertEqual(case['scope']['commitments'][0]['outdoor_until'], '21:00')
            audit_out = subprocess.run(base + ['audit', '--db', dbpath], cwd=REPO, check=True, capture_output=True, text=True).stdout
            self.assertEqual(json.loads(audit_out)['status'], 'matched')


if __name__ == '__main__':
    unittest.main()
