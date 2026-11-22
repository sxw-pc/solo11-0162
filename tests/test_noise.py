import json
import unittest
from pathlib import Path
from petboard.store import connect
from petboard import acoustic, complaint
from petboard.report import report

ROOT = Path(__file__).parent.parent / 'fixtures'


class NoiseTest(unittest.TestCase):
    def test_peak_driven_chain_requires_attribution(self):
        db = connect(':memory:')
        try:
            complaint.import_data(db, json.loads((ROOT / 'complaint_case.json').read_text()))
            self.assertEqual(db.execute('SELECT state FROM complaint_decision').fetchone()['state'], 'pending')
            samples = json.loads((ROOT / 'noise_case.json').read_text())['samples']
            acoustic.import_data(db, {'samples': [samples[0]]})
            self.assertEqual(db.execute('SELECT state FROM complaint_decision').fetchone()['state'], 'dismissed')
            acoustic.import_data(db, {'samples': [samples[1]]})
            # The old rule upheld the complaint on the 68 dB peak alone; a single
            # roadside peak is not an attributable sustained store dose.
            row = db.execute('SELECT * FROM complaint_decision').fetchone()
            self.assertEqual(row['state'], 'pending')
            plan = json.loads(row['required_plan'])
            self.assertEqual({entry['kind'] for entry in plan}, {'shutdown', 'barrier'})
            noise = db.execute('SELECT * FROM noise_decision').fetchone()
            self.assertEqual(noise['state'], 'uncertain')
            self.assertEqual(noise['peak_db'], 68.0)
            basis = json.loads(noise['basis'])
            self.assertAlmostEqual(basis['window_leq']['2026-11-02T20:00:00/2026-11-02T20:10:00'], 65.96, places=2)
            self.assertEqual(len(report(db)['noise_cases']), 1)
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
