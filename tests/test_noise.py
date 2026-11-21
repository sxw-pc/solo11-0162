import json
import unittest
from pathlib import Path
from petboard.store import connect
from petboard import acoustic, complaint
from petboard.report import report

ROOT = Path(__file__).parent.parent / 'fixtures'


class NoiseTest(unittest.TestCase):
    def test_acoustic_decision_drives_complaint(self):
        db = connect(':memory:')
        try:
            complaint.import_data(db, json.loads((ROOT / 'complaint_case.json').read_text()))
            self.assertEqual(db.execute('SELECT state FROM complaint_decision').fetchone()['state'], 'pending')
            acoustic.import_data(db, {'samples': [json.loads((ROOT / 'noise_case.json').read_text())['samples'][0]]})
            self.assertEqual(db.execute('SELECT state FROM complaint_decision').fetchone()['state'], 'dismissed')
            acoustic.import_data(db, {'samples': [json.loads((ROOT / 'noise_case.json').read_text())['samples'][1]]})
            self.assertEqual(db.execute('SELECT state FROM complaint_decision').fetchone()['state'], 'upheld')
            self.assertEqual(len(report(db)['noise_cases']), 1)
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
