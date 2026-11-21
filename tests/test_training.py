import json
import unittest
from pathlib import Path
from petboard.store import connect
from petboard import health, training
from petboard.report import report

ROOT = Path(__file__).parent.parent / 'fixtures'


class TrainingTest(unittest.TestCase):
    def test_training_trial_drives_graduation(self):
        db = connect(':memory:')
        try:
            health.import_data(db, json.loads((ROOT / 'intake_ok.json').read_text()))
            trial = json.loads((ROOT / 'training_case.json').read_text())
            training.import_data(db, trial)
            self.assertEqual(db.execute('SELECT state FROM graduation_decision').fetchone()['state'], 'graduated')
            attempt = dict(trial['trials'][0], event_id='trial-101-b', at='2026-11-03T14:00:00', successes=4)
            training.import_data(db, {'trials': [attempt]})
            self.assertEqual(db.execute('SELECT state FROM graduation_decision').fetchone()['state'], 'practice')
            self.assertEqual(len(report(db)['animals'][0]['training_trials']), 2)
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
